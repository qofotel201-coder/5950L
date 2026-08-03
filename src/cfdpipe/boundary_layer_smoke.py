"""Fail-closed quality gate for a complete boundary-layer smoke mesh.

This module contains no solver or post-processing integration.  It validates a
hash-bound, 48-surface wall contract, audits a linear Prism6/Tet4/Pyramid5
mixed mesh, parses the serialized SU2 mesh independently, and provides a
two-session Gmsh builder framework.  The geometry-specific shell extrusion and
core reconstruction are deliberately injected as a strategy so that an
unqualified fallback can never be selected silently.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import traceback
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from uuid import uuid4

from .boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
    validate_boundary_layer_schedule_binding_for_strategy,
)
from .coarse_schedule_feasibility import (
    CoarseScheduleFeasibilityError,
    apply_minimum_growth_endpoint,
    validate_minimum_growth_endpoint,
)
from .coarse_direction_feasibility import (
    CoarseDirectionFeasibilityError,
    COLLAR_REFINEMENT_ENDPOINT_SCHEMA,
    FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    FRONTIER_PATTERN_ENDPOINT_SCHEMA,
    PATTERN_ENDPOINT_SCHEMA,
    TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_frontier_collar_refinement_endpoint,
    make_owner_free_direction_frontier_fine_refinement_pattern_endpoint,
    make_owner_free_direction_frontier_pattern_endpoint,
    make_owner_free_direction_frontier_triple_refinement_pattern_endpoint,
    validate_owner_free_direction_endpoint,
)
from .coarse_component_direction import (
    DISCOVERY_SCHEMA as COMPONENT_DIRECTION_DISCOVERY_SCHEMA,
    SEARCH_SCHEMA as COMPONENT_DIRECTION_SEARCH_SCHEMA,
    CoarseComponentDirectionError,
    build_owner_free_components,
)
from .coarse_direction_replay import (
    CoarseDirectionReplayError,
    HOMOTOPY_DISCOVERY_SCHEMA,
    HOMOTOPY_ENDPOINT_SCHEMA,
    interpolate_physical_schedule_homotopy,
    validate_direction_replay_approval,
    validate_physical_schedule_homotopy_endpoint,
)
from .coarse_schedule_continuation import (
    DISCOVERY_SCHEMA as SCHEDULE_DIRECTION_CONTINUATION_DISCOVERY_SCHEMA,
    ENDPOINT_SCHEMA as SCHEDULE_DIRECTION_CONTINUATION_ENDPOINT_SCHEMA,
    CoarseScheduleContinuationError,
    validate_frontier_direction_endpoint,
)


PathLike = str | os.PathLike[str]
_SCHEMA = "cfdpipe.boundary_layer_smoke.v2"
_MAX_3D_ELEMENTS = 300_000
_REQUIRED_WALL_MEMBER_COUNT = 48
_REQUIRED_LAYER_COUNT = 15
_ALLOWED_3D_TYPES = ("Prism 6", "Pyramid 5", "Tetrahedron 4")
_NORMAL_MODES = ("implicit", "scalar", "vector")
_QUALITY_IMPROVEMENT_MODE = "stable_surface_threshold_min_v1"
_SCOPED_OPTIMIZER_POLICY = "disabled_no_safe_entity_scope"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE = math.ulp(1.0)
_FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP = 2187
_FINE_REFINEMENT_STATIC_QUALITY_EVALUATION_CAP = 738
_TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP = 3723
_TRIPLE_REFINEMENT_QUALITY_EVALUATION_CAP = 1536
_COLLAR_REFINEMENT_SELECTION_SCHEMA = (
    "cfdpipe.coarse_schedule_frontier_collar_refinement_selection.v1"
)
_SU2_VOLUME_CODES = {10: ("Tetrahedron 4", 4), 13: ("Prism 6", 6), 14: ("Pyramid 5", 5)}
_SU2_BOUNDARY_CODES = {5: ("Triangle 3", 3), 9: ("Quadrilateral 4", 4)}


class BoundaryLayerSmokeError(RuntimeError):
    """Raised whenever the complete smoke-mesh quality gate fails closed."""


class BoundaryLayerSmokeStrategy(Protocol):
    """Geometry-specific strategy used by :func:`build_boundary_layer_smoke`.

    ``build`` must construct the complete in-memory model and return the pure
    mesh-audit payload accepted by :func:`audit_mixed_mesh`.  ``readback`` is
    called in a new Gmsh session after the written MSH has been opened and must
    return the same kind of payload.  Keeping these methods explicit prevents a
    partial prism shell from being mistaken for a complete fluid domain.
    """

    def build(
        self,
        gmsh: Any,
        source_brep: Path,
        normalized_config: Mapping[str, Any],
        staging_directory: Path,
    ) -> Mapping[str, Any]: ...

    def readback(
        self,
        gmsh: Any,
        mesh_path: Path,
        normalized_config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _approved_ambiguous_owner_records(raw: Any) -> list[dict[str, str]]:
    """Validate stable owner choices against a retained PASS mesh manifest.

    The configured records never contain runtime entity or node tags.  A prior
    PASS manifest is used only to prove that its preferred owner maps to the
    configured stable root-coordinate digest on the configured stable source
    triangle and that the same owner has an independently recorded strict
    nonpositive source-triangle lineage in that manifest.
    """

    if not isinstance(raw, list) or len(raw) > 16:
        raise BoundaryLayerSmokeError(
            "approved ambiguous-owner evidence must be a bounded list"
        )
    expected_fields = {
        "source_triangle_sha256",
        "wall_surface_fingerprint",
        "owner_root_coordinate_sha256",
        "evidence_manifest_path",
        "evidence_manifest_sha256",
    }
    repository_root = Path(__file__).resolve().parents[2]
    trusted_root = (repository_root / "runs" / "boundary_layer_quality").resolve(
        strict=True
    )
    normalized: list[dict[str, str]] = []
    seen_triangles: set[str] = set()
    for index, record in enumerate(raw):
        if not isinstance(record, Mapping) or set(record) != expected_fields:
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} is incomplete"
            )
        triangle_id = str(record["source_triangle_sha256"]).casefold()
        wall_fingerprint = str(record["wall_surface_fingerprint"]).casefold()
        owner_id = str(record["owner_root_coordinate_sha256"]).casefold()
        manifest_hash = str(record["evidence_manifest_sha256"]).casefold()
        requested_path = str(record["evidence_manifest_path"]).strip()
        lexical_path = Path(requested_path)
        if (
            triangle_id in seen_triangles
            or _SHA256.fullmatch(triangle_id) is None
            or _SHA256.fullmatch(wall_fingerprint) is None
            or _SHA256.fullmatch(owner_id) is None
            or _SHA256.fullmatch(manifest_hash) is None
            or not requested_path
            or lexical_path.is_absolute()
            or ".." in lexical_path.parts
        ):
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} is invalid"
            )
        manifest_path = (repository_root / lexical_path).resolve(strict=True)
        if trusted_root not in manifest_path.parents or manifest_path.suffix != ".json":
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} evidence path is untrusted"
            )
        if _sha256(manifest_path) != manifest_hash:
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} evidence hash is stale"
            )
        try:
            evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
            post_repair = evidence["strategy_evidence"]["build"][
                "post_mesh_repair"
            ]
            triangle_records = post_repair["full_layer_detection"][
                "triangle_evidence"
            ]
            cone_records = post_repair["one_layer_chebyshev"]["records"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} evidence is malformed"
            ) from exc
        if (
            evidence.get("schema") != _SCHEMA
            or evidence.get("status") != "PASS"
            or evidence.get("diagnostic_quality_status") != "PASS"
            or evidence.get("requires_quality_improvement_before_production")
            is not False
            or evidence.get("source_unchanged") is not True
        ):
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} evidence is not a "
                "source-preserving diagnostic-quality PASS"
            )
        matching_triangles = [
            item
            for item in triangle_records
            if isinstance(item, Mapping)
            and str(item.get("source_triangle_sha256", "")).casefold() == triangle_id
            and str(item.get("wall_surface_fingerprint", "")).casefold()
            == wall_fingerprint
            and item.get("assignment_method")
            == "contains_unique_strict_failure_preferred_root"
        ]
        if len(matching_triangles) != 1:
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} lacks one strict owner"
            )
        owner_tag = matching_triangles[0].get("sharp_root_tag_audit")
        matching_roots = [
            item
            for item in cone_records
            if isinstance(item, Mapping)
            and item.get("base_node_tag_audit") == owner_tag
            and str(item.get("stable_root_coordinate_sha256", "")).casefold()
            == owner_id
        ]
        if len(matching_roots) != 1:
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} root evidence is inconsistent"
            )
        strict_triangle_ids = {
            str(item.get("source_triangle_sha256", "")).casefold()
            for item in post_repair["full_layer_detection"].get("bad_records", [])
            if isinstance(item, Mapping)
            and item.get("strict_nonpositive_quality") is True
            and owner_tag in item.get("source_root_tags_audit", [])
        }
        strict_owner_records = [
            item
            for item in triangle_records
            if isinstance(item, Mapping)
            and str(item.get("source_triangle_sha256", "")).casefold()
            in strict_triangle_ids
            and item.get("sharp_root_tag_audit") == owner_tag
        ]
        if (
            not strict_triangle_ids
            or len(strict_owner_records) != 1
            or owner_tag
            not in strict_owner_records[0].get("source_root_tags_audit", [])
            or strict_owner_records[0].get("assignment_method")
            not in {
                "contains_unique_sharp_root",
                "contains_unique_strict_failure_preferred_root",
            }
        ):
            raise BoundaryLayerSmokeError(
                f"approved ambiguous-owner record {index} lacks strict-failure lineage"
            )
        seen_triangles.add(triangle_id)
        normalized.append(
            {
                "source_triangle_sha256": triangle_id,
                "wall_surface_fingerprint": wall_fingerprint,
                "owner_root_coordinate_sha256": owner_id,
                "evidence_manifest_path": lexical_path.as_posix(),
                "evidence_manifest_sha256": manifest_hash,
            }
        )
    return sorted(normalized, key=lambda item: item["source_triangle_sha256"])


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerSmokeError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerSmokeError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise BoundaryLayerSmokeError(f"{label} contains NaN or Inf")
    return result


def _vector_subtract(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    if len(left) != 3 or len(right) != 3:
        raise BoundaryLayerSmokeError("3-D vector has an invalid length")
    return tuple(float(left[index]) - float(right[index]) for index in range(3))


def _vector_add(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    if len(left) != 3 or len(right) != 3:
        raise BoundaryLayerSmokeError("3-D vector has an invalid length")
    result = tuple(float(left[index]) + float(right[index]) for index in range(3))
    if not all(math.isfinite(value) for value in result):
        raise BoundaryLayerSmokeError("3-D vector sum is non-finite")
    return result


def _vector_scale(
    vector: Sequence[float], factor: float
) -> tuple[float, float, float]:
    if len(vector) != 3 or not math.isfinite(float(factor)):
        raise BoundaryLayerSmokeError("3-D vector scale is invalid")
    return tuple(float(value) * float(factor) for value in vector)


def _vector_dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != 3 or len(right) != 3:
        raise BoundaryLayerSmokeError("3-D vector has an invalid length")
    value = sum(float(left[index]) * float(right[index]) for index in range(3))
    if not math.isfinite(value):
        raise BoundaryLayerSmokeError("3-D vector dot product is non-finite")
    return value


def _vector_cross(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    if len(left) != 3 or len(right) != 3:
        raise BoundaryLayerSmokeError("3-D vector has an invalid length")
    result = (
        float(left[1]) * float(right[2]) - float(left[2]) * float(right[1]),
        float(left[2]) * float(right[0]) - float(left[0]) * float(right[2]),
        float(left[0]) * float(right[1]) - float(left[1]) * float(right[0]),
    )
    if not all(math.isfinite(value) for value in result):
        raise BoundaryLayerSmokeError("3-D vector cross product is non-finite")
    return result


def _unit_vector(vector: Sequence[float]) -> tuple[float, float, float] | None:
    squared = _vector_dot(vector, vector)
    if squared <= 1.0e-30:
        return None
    return _vector_scale(vector, 1.0 / math.sqrt(squared))


def _chebyshev_cone_direction(
    raw_normals: Sequence[Sequence[float]],
) -> tuple[float, tuple[float, float, float], int]:
    """Return the deterministic max-min unit direction for oriented normals.

    In three dimensions an optimum is represented by a single active normal,
    a two-normal angle bisector, or the equal-dot intersection of three active
    planes.  Enumerating all three cases avoids the incomplete triple-only
    search used by the exploratory probe and keeps the production repair free
    of runtime entity or node-tag selection.
    """

    normals: list[tuple[float, float, float]] = []
    for raw in raw_normals:
        normal = _unit_vector(raw)
        if normal is None:
            raise BoundaryLayerSmokeError("cone constraint contains a zero normal")
        normals.append(normal)
    if not normals:
        raise BoundaryLayerSmokeError("cone direction requires at least one normal")

    candidates: dict[tuple[float, float, float], tuple[float, float, float]] = {}

    def add_candidate(raw: Sequence[float]) -> None:
        candidate = _unit_vector(raw)
        if candidate is None:
            return
        for direction in (candidate, _vector_scale(candidate, -1.0)):
            key = tuple(round(value, 15) for value in direction)
            candidates[key] = direction

    for normal in normals:
        add_candidate(normal)
    for first_index, first in enumerate(normals):
        for second in normals[first_index + 1 :]:
            add_candidate(
                tuple(first[axis] + second[axis] for axis in range(3))
            )
            difference = _vector_subtract(first, second)
            if _unit_vector(
                tuple(first[axis] + second[axis] for axis in range(3))
            ) is None:
                for axis in (
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0),
                ):
                    add_candidate(_vector_cross(difference, axis))
    for first_index, first in enumerate(normals):
        for second_index in range(first_index + 1, len(normals)):
            second = normals[second_index]
            for third in normals[second_index + 1 :]:
                add_candidate(
                    _vector_cross(
                        _vector_subtract(first, second),
                        _vector_subtract(first, third),
                    )
                )
    if not candidates:
        raise BoundaryLayerSmokeError("cone direction produced no finite candidates")

    ranked = [
        (
            min(_vector_dot(normal, candidate) for normal in normals),
            key,
            candidate,
        )
        for key, candidate in candidates.items()
    ]
    margin, _key, direction = max(ranked, key=lambda item: (item[0], item[1]))
    if not math.isfinite(margin):
        raise BoundaryLayerSmokeError("cone direction margin is non-finite")
    return margin, direction, len(candidates)


def _closest_feasible_cone_direction(
    target_direction: Sequence[float],
    raw_normals: Sequence[Sequence[float]],
    original_direction: Sequence[float],
    *,
    interior_weight: float,
) -> tuple[tuple[float, float, float], float, float, int]:
    """Project a target direction into a 3-D incident-face half-space cone.

    The nearest spherical feasible point is attained either by the target, by
    its projection onto one active plane, or by the intersection of two active
    planes.  All of those cases are enumerated, filtered against every incident
    constraint, and tie-broken deterministically.  The selected boundary point
    is then blended with the already feasible one-layer direction so that the
    final direction lies strictly inside the cone.
    """

    target = _unit_vector(target_direction)
    original = _unit_vector(original_direction)
    if target is None or original is None:
        raise BoundaryLayerSmokeError("cone projection direction is zero")
    weight = _finite(interior_weight, "smoothing interior weight")
    if not 0.0 < weight < 1.0:
        raise BoundaryLayerSmokeError("smoothing interior weight must be between 0 and 1")
    normals: list[tuple[float, float, float]] = []
    for raw in raw_normals:
        normal = _unit_vector(raw)
        if normal is None:
            raise BoundaryLayerSmokeError("cone constraint contains a zero normal")
        normals.append(normal)
    if not normals:
        raise BoundaryLayerSmokeError("cone projection requires incident normals")

    candidates: dict[tuple[float, float, float], tuple[float, float, float]] = {}

    def add_candidate(raw: Sequence[float]) -> None:
        candidate = _unit_vector(raw)
        if candidate is None:
            return
        candidates[tuple(round(value, 15) for value in candidate)] = candidate

    add_candidate(original)
    add_candidate(target)
    for normal in normals:
        add_candidate(
            _vector_subtract(target, _vector_scale(normal, _vector_dot(target, normal)))
        )
    for first_index, first in enumerate(normals):
        for second in normals[first_index + 1 :]:
            intersection = _unit_vector(_vector_cross(first, second))
            if intersection is not None:
                add_candidate(intersection)
                add_candidate(_vector_scale(intersection, -1.0))

    feasibility_tolerance = 1.0e-9
    feasible = [
        (candidate, key)
        for key, candidate in candidates.items()
        if min(_vector_dot(normal, candidate) for normal in normals)
        >= -feasibility_tolerance
    ]
    if not feasible:
        raise BoundaryLayerSmokeError("incident-face direction cone has no feasible candidate")
    boundary, _key = max(
        feasible,
        key=lambda item: (_vector_dot(item[0], target), item[1]),
    )
    repaired = _unit_vector(
        _vector_add(
            _vector_scale(boundary, 1.0 - weight),
            _vector_scale(original, weight),
        )
    )
    if repaired is None:
        raise BoundaryLayerSmokeError("cone projection interior blend is zero")
    margin = min(_vector_dot(normal, repaired) for normal in normals)
    alignment = _vector_dot(repaired, target)
    if not math.isfinite(margin) or margin <= 0.0 or not math.isfinite(alignment):
        raise BoundaryLayerSmokeError(
            f"cone projection did not enter the strict feasible interior: {margin}"
        )
    return repaired, margin, alignment, len(candidates)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerSmokeError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BoundaryLayerSmokeError(f"{label} must be a non-negative integer")
    return value


def _hash_record(records: Mapping[str, Mapping[str, Any]], name: str) -> str:
    record = records.get(name)
    value = record.get("sha256") if isinstance(record, Mapping) else None
    normalized = str(value).casefold() if isinstance(value, str) else ""
    if _SHA256.fullmatch(normalized) is None:
        raise BoundaryLayerSmokeError(f"input record {name!r} has no SHA-256")
    return normalized


def _wall_contract(marker_config: Mapping[str, Any]) -> tuple[str, list[str], list[str]]:
    markers = marker_config.get("solver_markers")
    if not isinstance(markers, list):
        raise BoundaryLayerSmokeError("marker config has no solver_markers list")
    wall_markers: list[tuple[str, list[str]]] = []
    solver_names: list[str] = []
    for raw_marker in markers:
        if not isinstance(raw_marker, Mapping):
            raise BoundaryLayerSmokeError("marker config contains an invalid marker")
        name = str(raw_marker.get("physical_name", "")).strip()
        if not name:
            raise BoundaryLayerSmokeError("solver marker has no physical_name")
        if name in solver_names:
            raise BoundaryLayerSmokeError("solver marker names are not unique")
        solver_names.append(name)
        if raw_marker.get("kind") != "wall":
            continue
        members = raw_marker.get("members")
        if not isinstance(members, list) or not members:
            raise BoundaryLayerSmokeError("wall marker has no members")
        fingerprints: list[str] = []
        for member in members:
            if not isinstance(member, Mapping):
                raise BoundaryLayerSmokeError("wall marker member is invalid")
            fingerprint = str(member.get("fingerprint_id", "")).casefold()
            if _SHA256.fullmatch(fingerprint) is None:
                raise BoundaryLayerSmokeError("wall member has an invalid fingerprint")
            if any(key in member for key in ("tag", "entity_tag", "surface_tag")):
                raise BoundaryLayerSmokeError("wall member stores a runtime entity tag")
            fingerprints.append(fingerprint)
        if len(fingerprints) != len(set(fingerprints)):
            raise BoundaryLayerSmokeError("wall fingerprints are not unique")
        wall_markers.append((name, fingerprints))
    if len(wall_markers) != 1:
        raise BoundaryLayerSmokeError("exactly one wall marker is required")
    name, fingerprints = wall_markers[0]
    if len(fingerprints) != _REQUIRED_WALL_MEMBER_COUNT:
        raise BoundaryLayerSmokeError(
            f"wall marker must contain exactly {_REQUIRED_WALL_MEMBER_COUNT} members"
        )
    return name, sorted(fingerprints), sorted(solver_names)


def normalize_boundary_layer_smoke_config(
    document: Mapping[str, Any],
    *,
    project: Mapping[str, Any],
    marker_config: Mapping[str, Any],
    input_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and canonically normalize the complete smoke-mesh contract."""

    base_root = {
        "schema",
        "status",
        "policy",
        "provenance",
        "selection",
        "mesh",
        "post_mesh_repair",
        "quality_improvement",
    }
    if set(document) != base_root:
        raise BoundaryLayerSmokeError("boundary-layer smoke config root is incomplete")
    if document.get("schema") != _SCHEMA or document.get("status") != "CONFIGURED":
        raise BoundaryLayerSmokeError("boundary-layer smoke schema/status is invalid")

    policy = document.get("policy")
    expected_policy = {
        "smoke_only",
        "production_mesh_eligible",
        "run_su2",
        "run_paraview",
        "runtime_tags_present",
        "max_3d_elements",
    }
    if not isinstance(policy, Mapping) or set(policy) != expected_policy:
        raise BoundaryLayerSmokeError("boundary-layer smoke policy is incomplete")
    if (
        policy.get("smoke_only") is not True
        or policy.get("production_mesh_eligible") is not False
        or policy.get("run_su2") is not False
        or policy.get("run_paraview") is not False
        or policy.get("runtime_tags_present") is not False
    ):
        raise BoundaryLayerSmokeError("boundary-layer smoke policy is unsafe")
    max_elements = _positive_int(policy.get("max_3d_elements"), "max_3d_elements")
    if max_elements > _MAX_3D_ELEMENTS:
        raise BoundaryLayerSmokeError("max_3d_elements exceeds the 300000 smoke cap")

    provenance = document.get("provenance")
    provenance_fields = {
        "project_sha256": "project",
        "cases_sha256": "cases",
        "markers_sha256": "markers",
        "topology_smoke_sha256": "topology_smoke",
        "pipeline_brep_sha256": "pipeline_geometry",
    }
    if not isinstance(provenance, Mapping) or set(provenance) != set(provenance_fields):
        raise BoundaryLayerSmokeError("boundary-layer smoke provenance is incomplete")
    normalized_hashes: dict[str, str] = {}
    for field, record_name in provenance_fields.items():
        actual = _hash_record(input_records, record_name)
        configured = str(provenance.get(field, "")).casefold()
        if configured != actual:
            raise BoundaryLayerSmokeError(f"boundary-layer smoke provenance {field} is stale")
        normalized_hashes[field] = actual

    wall_name, all_wall_fingerprints, solver_marker_names = _wall_contract(marker_config)
    selection = document.get("selection")
    expected_selection = {"wall_physical_name", "wall_surface_fingerprints"}
    if not isinstance(selection, Mapping) or set(selection) != expected_selection:
        raise BoundaryLayerSmokeError("boundary-layer smoke selection is incomplete")
    selected_name = str(selection.get("wall_physical_name", "")).strip()
    raw_selected = selection.get("wall_surface_fingerprints")
    if not isinstance(raw_selected, list):
        raise BoundaryLayerSmokeError("wall_surface_fingerprints must be a list")
    selected = [str(value).casefold() for value in raw_selected]
    if (
        selected_name != wall_name
        or len(selected) != _REQUIRED_WALL_MEMBER_COUNT
        or len(selected) != len(set(selected))
        or sorted(selected) != all_wall_fingerprints
    ):
        raise BoundaryLayerSmokeError("selection must include all 48 wall members exactly once")

    project_mesh = project.get("mesh")
    if not isinstance(project_mesh, Mapping):
        raise BoundaryLayerSmokeError("project mesh table is missing")
    project_layers = _positive_int(
        project_mesh.get("minimum_prism_layers"), "project minimum_prism_layers"
    )
    growth_ratio = _finite(
        project_mesh.get("initial_growth_ratio"), "project initial_growth_ratio"
    )
    if project_layers != _REQUIRED_LAYER_COUNT or growth_ratio <= 1.0:
        raise BoundaryLayerSmokeError("project must freeze exactly 15 layers and growth > 1")

    mesh = document.get("mesh")
    expected_mesh = {
        "layer_count_source",
        "growth_ratio_source",
        "element_order",
        "allowed_3d_element_types",
        "normal_mode",
        "first_layer_height_m",
        "construction_first_layer_height_m",
        "characteristic_length_m",
        "direction_probe_distance_m",
        "recombine",
    }
    if not isinstance(mesh, Mapping) or set(mesh) != expected_mesh:
        raise BoundaryLayerSmokeError("boundary-layer smoke mesh policy is incomplete")
    raw_allowed = mesh.get("allowed_3d_element_types")
    normal_mode = str(mesh.get("normal_mode", "")).strip()
    first_layer_height = _finite(
        mesh.get("first_layer_height_m"), "first_layer_height_m"
    )
    construction_first_layer_height = _finite(
        mesh.get("construction_first_layer_height_m"),
        "construction_first_layer_height_m",
    )
    characteristic_length = _finite(
        mesh.get("characteristic_length_m"), "characteristic_length_m"
    )
    direction_probe_distance = _finite(
        mesh.get("direction_probe_distance_m"), "direction_probe_distance_m"
    )
    if (
        mesh.get("layer_count_source") != "project.mesh.minimum_prism_layers"
        or mesh.get("growth_ratio_source") != "project.mesh.initial_growth_ratio"
        or mesh.get("element_order") != 1
        or not isinstance(raw_allowed, list)
        or tuple(sorted(str(value) for value in raw_allowed))
        != tuple(sorted(_ALLOWED_3D_TYPES))
        or normal_mode not in _NORMAL_MODES
        or first_layer_height <= 0.0
        or construction_first_layer_height < first_layer_height
        or construction_first_layer_height
        > first_layer_height * sum(growth_ratio**index for index in range(project_layers))
        or characteristic_length <= 0.0
        or direction_probe_distance <= 0.0
        or mesh.get("recombine") is not True
    ):
        raise BoundaryLayerSmokeError("boundary-layer smoke mesh policy is unsafe")

    repair = document.get("post_mesh_repair")
    expected_repair = {
        "mode",
        "expected_initial_bad_prism_count",
        "expected_negative_volume_prism_count",
        "expected_cone_node_count",
        "expected_full_layer_bad_prism_count",
        "expected_initial_below_threshold_prism_count",
        "expected_bad_source_triangle_count",
        "expected_smoothing_group_count",
        "expected_smoothed_root_count",
        "smoothing_interior_weight",
        "minimum_smoothing_margin",
        "minimum_cone_margin",
        "height_tolerance_m",
        "approved_ambiguous_owners",
    }
    if not isinstance(repair, Mapping) or set(repair) != expected_repair:
        raise BoundaryLayerSmokeError("boundary-layer post-mesh repair is incomplete")
    repair_mode = str(repair.get("mode", "")).strip()
    initial_bad_count = _positive_int(
        repair.get("expected_initial_bad_prism_count"),
        "expected_initial_bad_prism_count",
    )
    negative_volume_count = _positive_int(
        repair.get("expected_negative_volume_prism_count"),
        "expected_negative_volume_prism_count",
    )
    cone_node_count = _positive_int(
        repair.get("expected_cone_node_count"), "expected_cone_node_count"
    )
    full_layer_bad_count = _nonnegative_int(
        repair.get("expected_full_layer_bad_prism_count"),
        "expected_full_layer_bad_prism_count",
    )
    initial_below_threshold_count = _positive_int(
        repair.get("expected_initial_below_threshold_prism_count"),
        "expected_initial_below_threshold_prism_count",
    )
    bad_source_triangle_count = _positive_int(
        repair.get("expected_bad_source_triangle_count"),
        "expected_bad_source_triangle_count",
    )
    smoothing_group_count = _positive_int(
        repair.get("expected_smoothing_group_count"),
        "expected_smoothing_group_count",
    )
    smoothed_root_count = _positive_int(
        repair.get("expected_smoothed_root_count"),
        "expected_smoothed_root_count",
    )
    smoothing_interior_weight = _finite(
        repair.get("smoothing_interior_weight"), "smoothing_interior_weight"
    )
    minimum_smoothing_margin = _finite(
        repair.get("minimum_smoothing_margin"), "minimum_smoothing_margin"
    )
    cone_margin = _finite(repair.get("minimum_cone_margin"), "minimum_cone_margin")
    height_tolerance = _finite(
        repair.get("height_tolerance_m"), "height_tolerance_m"
    )
    approved_ambiguous_owners = _approved_ambiguous_owner_records(
        repair.get("approved_ambiguous_owners")
    )
    if (
        repair_mode != "one_layer_orientation_cone_subdivision_v1"
        or initial_bad_count < negative_volume_count
        or bad_source_triangle_count
        > initial_below_threshold_count + full_layer_bad_count
        or smoothing_group_count > bad_source_triangle_count
        or smoothed_root_count < smoothing_group_count
        or not 0.0 < smoothing_interior_weight < 1.0
        or not 0.0 < minimum_smoothing_margin <= 1.0
        or not 0.0 < cone_margin <= 1.0
        or height_tolerance <= 0.0
        or height_tolerance >= first_layer_height
    ):
        raise BoundaryLayerSmokeError("boundary-layer post-mesh repair is unsafe")

    quality_improvement = document.get("quality_improvement")
    expected_quality_improvement = {
        "mode",
        "base_refinement_source",
        "base_excluded_surface_fingerprints",
        "additional_refinement_groups",
        "minimum_core_tetra_gamma",
        "maximum_core_tetra_below_gamma_count",
        "minimum_prism_scaled_jacobian",
        "maximum_prism_below_threshold_element_count",
        "maximum_prism_below_threshold_column_count",
        "core_tetra_meshing_algorithm",
        "core_tetra_meshing_algorithm_name",
        "scoped_optimizer_policy",
        "scoped_optimizer_evidence",
    }
    if (
        not isinstance(quality_improvement, Mapping)
        or set(quality_improvement) != expected_quality_improvement
    ):
        raise BoundaryLayerSmokeError(
            "boundary-layer quality-improvement contract is incomplete"
        )
    if (
        quality_improvement.get("mode") != _QUALITY_IMPROVEMENT_MODE
        or quality_improvement.get("base_refinement_source")
        != "config/topology_smoke.toml.local_refinement"
        or quality_improvement.get("scoped_optimizer_policy")
        != _SCOPED_OPTIMIZER_POLICY
        or quality_improvement.get("core_tetra_meshing_algorithm") != 10
        or quality_improvement.get("core_tetra_meshing_algorithm_name") != "HXT"
    ):
        raise BoundaryLayerSmokeError(
            "boundary-layer quality-improvement policy is unsafe"
        )
    optimizer_evidence = str(
        quality_improvement.get("scoped_optimizer_evidence", "")
    ).strip()
    if not optimizer_evidence:
        raise BoundaryLayerSmokeError("scoped optimizer rejection evidence is missing")

    raw_excluded = quality_improvement.get("base_excluded_surface_fingerprints")
    if not isinstance(raw_excluded, list):
        raise BoundaryLayerSmokeError(
            "base refinement exclusions must be a list of stable fingerprints"
        )
    excluded = [str(value).casefold() for value in raw_excluded]
    if (
        len(excluded) != len(set(excluded))
        or any(_SHA256.fullmatch(value) is None for value in excluded)
        or not set(excluded) <= set(all_wall_fingerprints)
    ):
        raise BoundaryLayerSmokeError("base refinement exclusions are invalid")

    raw_groups = quality_improvement.get("additional_refinement_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise BoundaryLayerSmokeError(
            "additional refinement groups must be a nonempty list"
        )
    normalized_groups: list[dict[str, Any]] = []
    group_names: set[str] = set()
    grouped_fingerprints: set[str] = set()
    expected_group_fields = {
        "name",
        "surface_fingerprint_ids",
        "minimum_size_m",
        "distance_max_m",
        "sampling",
    }
    for index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, Mapping) or set(raw_group) != expected_group_fields:
            raise BoundaryLayerSmokeError(
                f"additional refinement group {index} is incomplete"
            )
        name = str(raw_group.get("name", "")).strip()
        raw_fingerprints = raw_group.get("surface_fingerprint_ids")
        if not name or name in group_names or not isinstance(raw_fingerprints, list):
            raise BoundaryLayerSmokeError(
                f"additional refinement group {index} has an invalid name or selection"
            )
        fingerprints = [str(value).casefold() for value in raw_fingerprints]
        if (
            not fingerprints
            or len(fingerprints) != len(set(fingerprints))
            or any(_SHA256.fullmatch(value) is None for value in fingerprints)
            or not set(fingerprints) <= set(all_wall_fingerprints)
            or grouped_fingerprints.intersection(fingerprints)
        ):
            raise BoundaryLayerSmokeError(
                f"additional refinement group {index} fingerprints are invalid"
            )
        minimum_size = _finite(
            raw_group.get("minimum_size_m"),
            f"additional refinement group {index} minimum_size_m",
        )
        distance_max = _finite(
            raw_group.get("distance_max_m"),
            f"additional refinement group {index} distance_max_m",
        )
        sampling = _positive_int(
            raw_group.get("sampling"),
            f"additional refinement group {index} sampling",
        )
        if (
            minimum_size <= 0.0
            or minimum_size > characteristic_length
            or distance_max <= 0.0
        ):
            raise BoundaryLayerSmokeError(
                f"additional refinement group {index} sizing is unsafe"
            )
        group_names.add(name)
        grouped_fingerprints.update(fingerprints)
        normalized_groups.append(
            {
                "name": name,
                "surface_fingerprint_ids": sorted(fingerprints),
                "minimum_size_m": minimum_size,
                "distance_max_m": distance_max,
                "sampling": sampling,
            }
        )
    if not set(excluded) <= grouped_fingerprints:
        raise BoundaryLayerSmokeError(
            "every base refinement exclusion must be restored by an additional group"
        )

    minimum_core_gamma = _finite(
        quality_improvement.get("minimum_core_tetra_gamma"),
        "minimum_core_tetra_gamma",
    )
    minimum_prism_sj = _finite(
        quality_improvement.get("minimum_prism_scaled_jacobian"),
        "minimum_prism_scaled_jacobian",
    )
    maximum_core_low = _nonnegative_int(
        quality_improvement.get("maximum_core_tetra_below_gamma_count"),
        "maximum_core_tetra_below_gamma_count",
    )
    maximum_prism_low_elements = _nonnegative_int(
        quality_improvement.get("maximum_prism_below_threshold_element_count"),
        "maximum_prism_below_threshold_element_count",
    )
    maximum_prism_low_columns = _nonnegative_int(
        quality_improvement.get("maximum_prism_below_threshold_column_count"),
        "maximum_prism_below_threshold_column_count",
    )
    if (
        not 0.0 < minimum_core_gamma < 1.0
        or not 0.0 < minimum_prism_sj < 1.0
        or maximum_core_low != 0
        or maximum_prism_low_elements != 0
        or maximum_prism_low_columns != 0
    ):
        raise BoundaryLayerSmokeError(
            "quality-improvement hard thresholds may not be relaxed"
        )

    normalized: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "CONFIGURED",
        "smoke_only": True,
        "production_mesh_eligible": False,
        "diagnostic_quality_status": "PENDING",
        "requires_quality_improvement_before_production": None,
        "su2_called": False,
        "paraview_called": False,
        "runtime_tags_present": False,
        "max_3d_elements": max_elements,
        "provenance": normalized_hashes,
        "wall_physical_name": wall_name,
        "wall_surface_fingerprints": all_wall_fingerprints,
        "wall_member_count": len(all_wall_fingerprints),
        "solver_marker_names": solver_marker_names,
        "layer_count": project_layers,
        "growth_ratio": growth_ratio,
        "element_order": 1,
        "allowed_3d_element_types": list(_ALLOWED_3D_TYPES),
        "normal_mode": normal_mode,
        "first_layer_height_m": first_layer_height,
        "construction_first_layer_height_m": construction_first_layer_height,
        "characteristic_length_m": characteristic_length,
        "direction_probe_distance_m": direction_probe_distance,
        "recombine": True,
        "post_mesh_repair": {
            "mode": repair_mode,
            "expected_initial_bad_prism_count": initial_bad_count,
            "expected_negative_volume_prism_count": negative_volume_count,
            "expected_cone_node_count": cone_node_count,
            "expected_full_layer_bad_prism_count": full_layer_bad_count,
            "expected_initial_below_threshold_prism_count": (
                initial_below_threshold_count
            ),
            "expected_bad_source_triangle_count": bad_source_triangle_count,
            "expected_smoothing_group_count": smoothing_group_count,
            "expected_smoothed_root_count": smoothed_root_count,
            "smoothing_interior_weight": smoothing_interior_weight,
            "minimum_smoothing_margin": minimum_smoothing_margin,
            "minimum_cone_margin": cone_margin,
            "height_tolerance_m": height_tolerance,
            "approved_ambiguous_owners": approved_ambiguous_owners,
        },
        "quality_improvement": {
            "mode": _QUALITY_IMPROVEMENT_MODE,
            "base_refinement_source": (
                "config/topology_smoke.toml.local_refinement"
            ),
            "base_excluded_surface_fingerprints": sorted(excluded),
            "additional_refinement_groups": sorted(
                normalized_groups, key=lambda value: value["name"]
            ),
            "minimum_core_tetra_gamma": minimum_core_gamma,
            "maximum_core_tetra_below_gamma_count": maximum_core_low,
            "minimum_prism_scaled_jacobian": minimum_prism_sj,
            "maximum_prism_below_threshold_element_count": (
                maximum_prism_low_elements
            ),
            "maximum_prism_below_threshold_column_count": (
                maximum_prism_low_columns
            ),
            "core_tetra_meshing_algorithm": 10,
            "core_tetra_meshing_algorithm_name": "HXT",
            "scoped_optimizer_policy": _SCOPED_OPTIMIZER_POLICY,
            "scoped_optimizer_evidence": optimizer_evidence,
        },
    }
    normalized["normalized_config_sha256"] = _canonical_hash(normalized)
    return normalized


def _node_tuple(value: object, expected: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BoundaryLayerSmokeError(f"{label} nodes must be a sequence")
    nodes: list[int] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BoundaryLayerSmokeError(f"{label} contains a non-integral node")
        nodes.append(raw)
    if len(nodes) != expected or len(nodes) != len(set(nodes)):
        raise BoundaryLayerSmokeError(f"{label} has invalid connectivity")
    return tuple(nodes)


def _faces(element_type: str, nodes: Sequence[int]) -> tuple[tuple[int, ...], ...]:
    if element_type == "Tetrahedron 4":
        n = _node_tuple(nodes, 4, element_type)
        raw = ((n[0], n[2], n[1]), (n[0], n[1], n[3]), (n[1], n[2], n[3]), (n[2], n[0], n[3]))
    elif element_type == "Prism 6":
        n = _node_tuple(nodes, 6, element_type)
        raw = (
            (n[0], n[1], n[2]),
            (n[3], n[5], n[4]),
            (n[0], n[3], n[4], n[1]),
            (n[1], n[4], n[5], n[2]),
            (n[2], n[5], n[3], n[0]),
        )
    elif element_type == "Pyramid 5":
        n = _node_tuple(nodes, 5, element_type)
        raw = (
            (n[0], n[3], n[2], n[1]),
            (n[0], n[1], n[4]),
            (n[1], n[2], n[4]),
            (n[2], n[3], n[4]),
            (n[3], n[0], n[4]),
        )
    else:
        raise BoundaryLayerSmokeError(f"unsupported 3D element type {element_type!r}")
    return tuple((len(face), *sorted(face)) for face in raw)


def _face_key(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BoundaryLayerSmokeError(f"{label} must be a node sequence")
    expected = len(value)
    if expected not in {3, 4}:
        raise BoundaryLayerSmokeError(f"{label} must be a triangle or quadrilateral")
    nodes = _node_tuple(value, expected, label)
    return (expected, *sorted(nodes))


def _derive_internal_role_faces(
    elements: Sequence[Mapping[str, Any]],
) -> tuple[list[list[int]], list[list[int]]]:
    """Return raw-node faces for the two internal mixed-mesh role audits.

    ``_face_key`` prefixes its canonical key with the face arity.  Role faces
    are passed back through ``_face_key`` by :func:`audit_mixed_mesh`, so the
    prefix must never be exposed as if it were a node tag.
    """

    face_elements: dict[tuple[int, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for element in elements:
        for face in element["faces"]:
            face_elements[_face_key(face, "role face")].append(element)
    prism_lateral_faces = [
        list(key[1:])
        for key, owners in face_elements.items()
        if key[0] == 4
        and len(owners) == 2
        and all(owner["type"] == "Prism 6" for owner in owners)
    ]
    transition_faces = [
        list(key[1:])
        for key, owners in face_elements.items()
        if len(owners) == 2 and any(owner["type"] == "Pyramid 5" for owner in owners)
    ]
    return prism_lateral_faces, transition_faces


def _normalize_columns(
    wall_columns: Mapping[str, object],
    collar_faces: Mapping[str, object],
    expected_fingerprints: set[str],
    prism_tags: set[int],
    layer_count: int,
) -> dict[str, Any]:
    if set(wall_columns) - expected_fingerprints or set(collar_faces) - expected_fingerprints:
        raise BoundaryLayerSmokeError("column/collar audit contains an unknown wall fingerprint")
    missing = expected_fingerprints - set(wall_columns) - set(collar_faces)
    if missing:
        raise BoundaryLayerSmokeError("one or more wall members have no prism or collar evidence")
    used_prisms: set[int] = set()
    column_count = 0
    per_surface: dict[str, dict[str, int]] = {}
    for fingerprint in sorted(expected_fingerprints):
        raw_columns = wall_columns.get(fingerprint, [])
        raw_collar = collar_faces.get(fingerprint, [])
        if not isinstance(raw_columns, Sequence) or isinstance(raw_columns, (str, bytes)):
            raise BoundaryLayerSmokeError("wall columns must be sequences")
        if not isinstance(raw_collar, Sequence) or isinstance(raw_collar, (str, bytes)):
            raise BoundaryLayerSmokeError("collar faces must be sequences")
        local_columns = 0
        for raw_column in raw_columns:
            if not isinstance(raw_column, Sequence) or isinstance(raw_column, (str, bytes)):
                raise BoundaryLayerSmokeError("prism column must be a sequence")
            tags = list(raw_column)
            if len(tags) != layer_count or any(
                isinstance(tag, bool) or not isinstance(tag, int) for tag in tags
            ):
                raise BoundaryLayerSmokeError("each prism column must contain exactly 15 tags")
            if len(tags) != len(set(tags)) or not set(tags) <= prism_tags:
                raise BoundaryLayerSmokeError("prism column references invalid or duplicate tags")
            if used_prisms.intersection(tags):
                raise BoundaryLayerSmokeError("a prism belongs to multiple wall columns")
            used_prisms.update(tags)
            local_columns += 1
        if local_columns == 0 and len(raw_collar) == 0:
            raise BoundaryLayerSmokeError("wall member has neither prism coverage nor a collar")
        per_surface[fingerprint] = {
            "column_count": local_columns,
            "prism_count": local_columns * layer_count,
            "collar_face_count": len(raw_collar),
        }
        column_count += local_columns
    if used_prisms != prism_tags:
        raise BoundaryLayerSmokeError("one or more prisms have no unique 15-layer column")
    return {
        "column_count": column_count,
        "covered_prism_count": len(used_prisms),
        "per_wall_surface": per_surface,
    }


def audit_mixed_mesh(
    *,
    nodes: Mapping[int, Sequence[float]],
    elements: Sequence[Mapping[str, Any]],
    marker_faces: Mapping[str, Sequence[Sequence[int]]],
    shared_interface_faces: Sequence[Sequence[int]],
    quality: Mapping[int, Mapping[str, Any]],
    wall_columns: Mapping[str, object],
    collar_faces: Mapping[str, object],
    wall_base_faces: Mapping[str, Sequence[Sequence[int]]] | None = None,
    prism_base_faces: Mapping[str, Sequence[Sequence[int]]] | None = None,
    collar_base_faces: Mapping[str, Sequence[Sequence[int]]] | None = None,
    role_faces: Mapping[str, Sequence[Sequence[int]]] | None = None,
    wall_fingerprints: Sequence[str],
    expected_marker_names: Iterable[str],
    requested_layers: int = _REQUIRED_LAYER_COUNT,
    max_elements: int = _MAX_3D_ELEMENTS,
    contract_mode: str = "smoke",
) -> dict[str, Any]:
    """Audit topology, ownership, markers, layers and quality of a mixed mesh."""

    layers = _positive_int(requested_layers, "requested_layers")
    if contract_mode not in {"smoke", "coarse_calibration"}:
        raise BoundaryLayerSmokeError("mixed-mesh audit contract mode is invalid")
    if contract_mode == "smoke" and layers != _REQUIRED_LAYER_COUNT:
        raise BoundaryLayerSmokeError("the complete smoke mesh requires exactly 15 layers")
    if contract_mode == "coarse_calibration" and layers < _REQUIRED_LAYER_COUNT:
        raise BoundaryLayerSmokeError("coarse calibration requires at least 15 layers")
    cap = _positive_int(max_elements, "max_elements")
    if contract_mode == "smoke" and cap > _MAX_3D_ELEMENTS:
        raise BoundaryLayerSmokeError("max_elements exceeds the 300000 smoke cap")
    if not nodes:
        raise BoundaryLayerSmokeError("mesh has no nodes")
    normalized_nodes: set[int] = set()
    for tag, coordinates in nodes.items():
        if isinstance(tag, bool) or not isinstance(tag, int) or tag < 0:
            raise BoundaryLayerSmokeError("mesh node tag is invalid")
        if not isinstance(coordinates, Sequence) or len(coordinates) != 3:
            raise BoundaryLayerSmokeError("mesh node must contain three coordinates")
        for axis, value in enumerate(coordinates):
            _finite(value, f"node {tag} coordinate {axis}")
        normalized_nodes.add(tag)

    if not isinstance(elements, Sequence) or not elements:
        raise BoundaryLayerSmokeError("mesh has no 3D elements")
    if len(elements) > cap:
        raise BoundaryLayerSmokeError("3D element count exceeds the configured cap")
    owners: dict[tuple[int, ...], list[tuple[int, str, str]]] = defaultdict(list)
    element_types: Counter[str] = Counter()
    element_tags: set[int] = set()
    element_regions: dict[int, str] = {}
    prism_tags: set[int] = set()
    for raw in elements:
        if not isinstance(raw, Mapping):
            raise BoundaryLayerSmokeError("3D element record is invalid")
        tag = raw.get("tag")
        if isinstance(tag, bool) or not isinstance(tag, int) or tag in element_tags:
            raise BoundaryLayerSmokeError("3D element tags must be unique integers")
        element_type = str(raw.get("type", ""))
        region = str(raw.get("region", "")).strip()
        if element_type not in _ALLOWED_3D_TYPES or not region:
            raise BoundaryLayerSmokeError("3D element type/region is invalid")
        connectivity = raw.get("nodes")
        _node_tuple(
            connectivity,
            {"Tetrahedron 4": 4, "Prism 6": 6, "Pyramid 5": 5}[element_type],
            element_type,
        )
        raw_faces = raw.get("faces")
        if raw_faces is None:
            element_faces = _faces(element_type, connectivity)
        else:
            if not isinstance(raw_faces, Sequence) or isinstance(raw_faces, (str, bytes)):
                raise BoundaryLayerSmokeError("official element faces must be a sequence")
            expected_face_count = {"Tetrahedron 4": 4, "Prism 6": 5, "Pyramid 5": 5}[
                element_type
            ]
            element_faces = tuple(
                _face_key(value, f"element {tag} official face") for value in raw_faces
            )
            if len(element_faces) != expected_face_count or len(set(element_faces)) != len(
                element_faces
            ):
                raise BoundaryLayerSmokeError("official element faces are incomplete")
        for face in element_faces:
            if any(node not in normalized_nodes for node in face[1:]):
                raise BoundaryLayerSmokeError("3D element references an unknown node")
            owners[face].append((tag, region, element_type))
        element_tags.add(tag)
        element_regions[tag] = region
        element_types[element_type] += 1
        if element_type == "Prism 6":
            prism_tags.add(tag)
    non_manifold = [face for face, values in owners.items() if len(values) not in {1, 2}]
    if non_manifold:
        raise BoundaryLayerSmokeError("mesh contains orphan or non-manifold volume faces")

    expected_markers = set(expected_marker_names)
    if set(marker_faces) != expected_markers:
        raise BoundaryLayerSmokeError("marker names do not exactly match the contract")
    marker_owner: dict[tuple[int, ...], str] = {}
    marker_counts: dict[str, int] = {}
    for name, faces in marker_faces.items():
        count = 0
        for raw_face in faces:
            face = _face_key(raw_face, f"marker {name!r} face")
            if face in marker_owner:
                raise BoundaryLayerSmokeError("a boundary face belongs to multiple markers")
            if len(owners.get(face, [])) != 1:
                raise BoundaryLayerSmokeError("every marker face must have one volume owner")
            marker_owner[face] = name
            count += 1
        if count == 0:
            raise BoundaryLayerSmokeError(f"marker {name!r} is empty")
        marker_counts[name] = count
    exterior = {face for face, values in owners.items() if len(values) == 1}
    if set(marker_owner) != exterior:
        missing = sorted(exterior - set(marker_owner))
        extra = sorted(set(marker_owner) - exterior)
        raise BoundaryLayerSmokeError(
            "external volume faces are not exactly covered by markers: "
            f"exterior={len(exterior)}, marked={len(marker_owner)}, "
            f"unmarked={len(missing)}, nonexternal_marked={len(extra)}, "
            f"unmarked_sample={missing[:8]!r}, "
            f"nonexternal_marked_sample={extra[:8]!r}"
        )

    interface_keys: set[tuple[int, ...]] = set()
    interface_region_pairs: Counter[tuple[str, str]] = Counter()
    for raw_face in shared_interface_faces:
        face = _face_key(raw_face, "shared interface face")
        if face in interface_keys or face in marker_owner:
            raise BoundaryLayerSmokeError("shared interface is duplicate or marked as a boundary")
        face_owners = owners.get(face, [])
        if len(face_owners) != 2 or face_owners[0][1] == face_owners[1][1]:
            raise BoundaryLayerSmokeError("shared interface must join two distinct regions")
        interface_keys.add(face)
        interface_region_pairs[tuple(sorted((face_owners[0][1], face_owners[1][1])))] += 1
    if not interface_keys:
        raise BoundaryLayerSmokeError("shared interface evidence is empty")

    role_audit: dict[str, int] = {}
    if role_faces is not None:
        required_roles = {
            "wall_base",
            "prism_core_top",
            "prism_prism_lateral",
            "transition",
        }
        if set(role_faces) != required_roles:
            raise BoundaryLayerSmokeError("interface role face audit is incomplete")

        def owner_classes(raw_face: Sequence[int], label: str) -> list[tuple[int, str, str]]:
            key = _face_key(raw_face, label)
            values = owners.get(key, [])
            if len(values) not in {1, 2}:
                raise BoundaryLayerSmokeError(f"{label} has invalid ownership")
            return values

        for raw_face in role_faces["wall_base"]:
            values = owner_classes(raw_face, "wall base face")
            if len(values) != 1 or values[0][1] != "boundary_layer" or values[0][2] != "Prism 6":
                collar_keys = {
                    _face_key(face, "approved collar base")
                    for faces in (collar_base_faces or {}).values()
                    for face in faces
                }
                if _face_key(raw_face, "wall base face") not in collar_keys:
                    raise BoundaryLayerSmokeError("wall base face is not owned by one prism")
                if (
                    len(values) != 1
                    or values[0][1] != "fluid_core_1"
                    or values[0][2] != "Tetrahedron 4"
                ):
                    raise BoundaryLayerSmokeError(
                        "approved collar base is not owned by one core tetrahedron"
                    )
        for raw_face in role_faces["prism_core_top"]:
            values = owner_classes(raw_face, "prism/core top face")
            classes = {(value[1], value[2]) for value in values}
            if len(values) != 2 or ("boundary_layer", "Prism 6") not in classes or not any(
                region == "fluid_core_1" and element_type in {"Tetrahedron 4", "Pyramid 5"}
                for _, region, element_type in values
            ):
                raise BoundaryLayerSmokeError("prism top does not join prism to core1")
        for raw_face in role_faces["prism_prism_lateral"]:
            values = owner_classes(raw_face, "prism lateral face")
            if len(values) != 2 or any(
                region != "boundary_layer" or element_type != "Prism 6"
                for _, region, element_type in values
            ):
                raise BoundaryLayerSmokeError("prism lateral face is not prism/prism")
        for raw_face in role_faces["transition"]:
            values = owner_classes(raw_face, "collar transition face")
            if len(values) != 2 or not any(value[2] == "Pyramid 5" for value in values):
                raise BoundaryLayerSmokeError("transition face has no Pyramid 5 owner")
        role_audit = {name: len(faces) for name, faces in role_faces.items()}

    if set(quality) != element_tags:
        raise BoundaryLayerSmokeError("quality records do not exactly cover 3D elements")
    quality_names = ("volume", "minDetJac", "minSJ", "minSICN")
    minima = {name: math.inf for name in quality_names}
    nonpositive_volume = 0
    nonpositive_jacobian = 0
    nonfinite = 0
    for tag in sorted(element_tags):
        record = quality[tag]
        if not isinstance(record, Mapping):
            raise BoundaryLayerSmokeError("quality record is invalid")
        try:
            volume = _finite(record.get("volume"), f"element {tag} volume")
            det_jacobian = _finite(
                record.get("minDetJac"), f"element {tag} minDetJac"
            )
            scaled_jacobian = _finite(
                record.get("minSJ"), f"element {tag} minSJ"
            )
            sicn = _finite(record.get("minSICN"), f"element {tag} minSICN")
        except BoundaryLayerSmokeError:
            nonfinite += 1
            raise
        nonpositive_volume += int(volume <= 0.0)
        nonpositive_jacobian += int(
            det_jacobian <= 0.0 or scaled_jacobian <= 0.0 or sicn <= 0.0
        )
        for name, value in (
            ("volume", volume),
            ("minDetJac", det_jacobian),
            ("minSJ", scaled_jacobian),
            ("minSICN", sicn),
        ):
            minima[name] = min(minima[name], value)
    if nonpositive_volume or nonpositive_jacobian:
        raise BoundaryLayerSmokeError("mesh contains non-positive volume or Jacobian")

    expected_fingerprints = {str(value).casefold() for value in wall_fingerprints}
    if len(expected_fingerprints) != _REQUIRED_WALL_MEMBER_COUNT or any(
        _SHA256.fullmatch(value) is None for value in expected_fingerprints
    ):
        raise BoundaryLayerSmokeError("wall fingerprint audit must contain 48 stable members")
    layer_audit = _normalize_columns(
        wall_columns,
        collar_faces,
        expected_fingerprints,
        prism_tags,
        layers,
    )
    coverage_audit: dict[str, Any] = {}
    if any(value is not None for value in (wall_base_faces, prism_base_faces, collar_base_faces)):
        if wall_base_faces is None or prism_base_faces is None or collar_base_faces is None:
            raise BoundaryLayerSmokeError("wall coverage face audit is incomplete")

        def face_area(raw_face: Sequence[int]) -> float:
            tags = list(raw_face)
            points = [nodes[int(tag)] for tag in tags]
            def triangle_area(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
                ab = [float(b[i]) - float(a[i]) for i in range(3)]
                ac = [float(c[i]) - float(a[i]) for i in range(3)]
                cross = (
                    ab[1] * ac[2] - ab[2] * ac[1],
                    ab[2] * ac[0] - ab[0] * ac[2],
                    ab[0] * ac[1] - ab[1] * ac[0],
                )
                return 0.5 * math.sqrt(sum(value * value for value in cross))
            area = triangle_area(points[0], points[1], points[2])
            if len(points) == 4:
                area += triangle_area(points[0], points[2], points[3])
            if not math.isfinite(area) or area <= 0.0:
                raise BoundaryLayerSmokeError("wall coverage face has non-positive area")
            return area

        expected_fp = expected_fingerprints
        if set(wall_base_faces) != expected_fp or set(prism_base_faces) != expected_fp:
            raise BoundaryLayerSmokeError("wall coverage does not contain all 48 members")
        if set(collar_base_faces) - expected_fp:
            raise BoundaryLayerSmokeError("collar base has an unknown wall fingerprint")
        for fingerprint in sorted(expected_fp):
            base_records = list(wall_base_faces[fingerprint])
            prism_records = list(prism_base_faces[fingerprint])
            collar_records = list(collar_base_faces.get(fingerprint, []))
            base = {_face_key(face, "wall base coverage") for face in base_records}
            prism = {_face_key(face, "prism base coverage") for face in prism_records}
            collar = {_face_key(face, "collar base coverage") for face in collar_records}
            if not prism:
                raise BoundaryLayerSmokeError("an entire wall surface has no prism coverage")
            if prism & collar or prism | collar != base:
                raise BoundaryLayerSmokeError("prism/collar base coverage is not exact and disjoint")
            base_area = sum(face_area(face) for face in base_records)
            prism_area = sum(face_area(face) for face in prism_records)
            collar_area = sum(face_area(face) for face in collar_records)
            coverage_audit[fingerprint] = {
                "base_face_count": len(base),
                "prism_base_face_count": len(prism),
                "collar_base_face_count": len(collar),
                "base_area_m2": base_area,
                "prism_area_m2": prism_area,
                "collar_area_m2": collar_area,
                "prism_coverage_fraction": prism_area / base_area,
                "total_coverage_fraction": (prism_area + collar_area) / base_area,
            }
    return {
        "status": "PASS",
        "node_count": len(normalized_nodes),
        "element_count_3d": len(element_tags),
        "element_types_3d": dict(sorted(element_types.items())),
        "allowed_element_types_only": True,
        "external_face_count": len(exterior),
        "internal_face_count": sum(len(values) == 2 for values in owners.values()),
        "non_manifold_face_count": 0,
        "marker_face_counts": dict(sorted(marker_counts.items())),
        "shared_interface_face_count": len(interface_keys),
        "shared_interface_region_pairs": {
            "|".join(pair): count for pair, count in sorted(interface_region_pairs.items())
        },
        "requested_layer_count": layers,
        "layer_audit": layer_audit,
        "wall_coverage": coverage_audit,
        "interface_role_face_counts": role_audit,
        "quality": {
            **minima,
            "nonpositive_volume_count": 0,
            "nonpositive_jacobian_count": 0,
            "nonfinite_count": nonfinite,
        },
    }


def _clean_su2_lines(path: Path) -> list[str]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise BoundaryLayerSmokeError("SU2 mesh is missing or empty")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BoundaryLayerSmokeError(f"cannot read SU2 mesh: {error}") from error
    lowered = text.casefold()
    if re.search(r"(?<![a-z])(nan|[-+]?inf(?:inity)?)(?![a-z])", lowered):
        raise BoundaryLayerSmokeError("SU2 mesh contains NaN or Inf")
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("%", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _assignment(line: str, expected: str) -> str:
    if "=" not in line:
        raise BoundaryLayerSmokeError(f"expected SU2 assignment {expected}")
    key, value = (part.strip() for part in line.split("=", 1))
    if key.casefold() != expected.casefold() or not value:
        raise BoundaryLayerSmokeError(f"expected SU2 assignment {expected}")
    return value


def parse_su2_mixed_mesh(
    path: PathLike,
    *,
    expected_markers: Iterable[str],
    max_elements: int = _MAX_3D_ELEMENTS,
) -> dict[str, Any]:
    """Independently parse a 3-D linear mixed SU2 mesh."""

    cap = _positive_int(max_elements, "max_elements")
    if cap > _MAX_3D_ELEMENTS:
        raise BoundaryLayerSmokeError("max_elements exceeds the 300000 smoke cap")
    lines = _clean_su2_lines(Path(path))
    cursor = 0
    try:
        ndime = int(_assignment(lines[cursor], "NDIME"))
        cursor += 1
        nelem = int(_assignment(lines[cursor], "NELEM"))
        cursor += 1
    except (IndexError, ValueError) as error:
        raise BoundaryLayerSmokeError("SU2 NDIME/NELEM is not parseable") from error
    if ndime != 3 or not 0 < nelem <= cap:
        raise BoundaryLayerSmokeError("SU2 dimension/element count is invalid")
    volume_counts: Counter[str] = Counter()
    volume_nodes: list[tuple[int, ...]] = []
    for index in range(nelem):
        if cursor >= len(lines):
            raise BoundaryLayerSmokeError("SU2 ended inside volume elements")
        try:
            tokens = [int(value) for value in lines[cursor].split()]
        except ValueError as error:
            raise BoundaryLayerSmokeError(f"SU2 element {index} is not integral") from error
        cursor += 1
        if not tokens or tokens[0] not in _SU2_VOLUME_CODES:
            raise BoundaryLayerSmokeError("SU2 contains a disallowed 3D element code")
        name, node_count = _SU2_VOLUME_CODES[tokens[0]]
        if len(tokens) not in {node_count + 1, node_count + 2}:
            raise BoundaryLayerSmokeError("SU2 volume element has an invalid field count")
        nodes = tuple(tokens[1 : 1 + node_count])
        if len(nodes) != len(set(nodes)) or any(node < 0 for node in nodes):
            raise BoundaryLayerSmokeError("SU2 volume element connectivity is invalid")
        volume_nodes.append(nodes)
        volume_counts[name] += 1
    try:
        npoin = int(_assignment(lines[cursor], "NPOIN"))
        cursor += 1
    except (IndexError, ValueError) as error:
        raise BoundaryLayerSmokeError("SU2 NPOIN is not parseable") from error
    if npoin <= 0:
        raise BoundaryLayerSmokeError("SU2 NPOIN must be positive")
    point_indices: set[int] = set()
    for index in range(npoin):
        if cursor >= len(lines):
            raise BoundaryLayerSmokeError("SU2 ended inside point records")
        tokens = lines[cursor].split()
        cursor += 1
        if len(tokens) not in {3, 4}:
            raise BoundaryLayerSmokeError("SU2 point has an invalid field count")
        for axis in range(3):
            _finite(tokens[axis], f"SU2 point {index} coordinate")
        point_tag = index if len(tokens) == 3 else int(tokens[3])
        if point_tag in point_indices or point_tag < 0:
            raise BoundaryLayerSmokeError("SU2 point indices are invalid")
        point_indices.add(point_tag)
    for connectivity in volume_nodes:
        if any(node not in point_indices for node in connectivity):
            raise BoundaryLayerSmokeError("SU2 volume element references an unknown point")
    try:
        nmark = int(_assignment(lines[cursor], "NMARK"))
        cursor += 1
    except (IndexError, ValueError) as error:
        raise BoundaryLayerSmokeError("SU2 NMARK is not parseable") from error
    marker_counts: dict[str, int] = {}
    boundary_counts: Counter[str] = Counter()
    for marker_index in range(nmark):
        try:
            name = _assignment(lines[cursor], "MARKER_TAG")
            cursor += 1
            count = int(_assignment(lines[cursor], "MARKER_ELEMS"))
            cursor += 1
        except (IndexError, ValueError) as error:
            raise BoundaryLayerSmokeError("SU2 marker header is not parseable") from error
        if not name or name in marker_counts or count <= 0:
            raise BoundaryLayerSmokeError("SU2 marker name/count is invalid")
        marker_counts[name] = count
        for element_index in range(count):
            if cursor >= len(lines):
                raise BoundaryLayerSmokeError("SU2 ended inside marker elements")
            try:
                tokens = [int(value) for value in lines[cursor].split()]
            except ValueError as error:
                raise BoundaryLayerSmokeError("SU2 marker element is not integral") from error
            cursor += 1
            if not tokens or tokens[0] not in _SU2_BOUNDARY_CODES:
                raise BoundaryLayerSmokeError("SU2 marker element type is invalid")
            boundary_name, node_count = _SU2_BOUNDARY_CODES[tokens[0]]
            if len(tokens) not in {node_count + 1, node_count + 2}:
                raise BoundaryLayerSmokeError("SU2 marker element field count is invalid")
            referenced = tokens[1 : 1 + node_count]
            if len(referenced) != len(set(referenced)) or any(
                node not in point_indices for node in referenced
            ):
                raise BoundaryLayerSmokeError("SU2 marker references an invalid point")
            boundary_counts[boundary_name] += 1
    if cursor != len(lines):
        raise BoundaryLayerSmokeError("SU2 mesh contains unexpected trailing records")
    expected = set(expected_markers)
    if set(marker_counts) != expected or nmark != len(expected):
        raise BoundaryLayerSmokeError("SU2 markers do not exactly match the contract")
    return {
        "status": "PASS",
        "ndime": ndime,
        "nelem": nelem,
        "npoin": npoin,
        "nmark": nmark,
        "volume_element_types": dict(sorted(volume_counts.items())),
        "boundary_element_types": dict(sorted(boundary_counts.items())),
        "marker_element_counts": dict(sorted(marker_counts.items())),
    }


def _oriented_surfaces(volume_record: Mapping[str, Any]) -> list[int]:
    raw = volume_record.get("oriented_boundary_surfaces")
    if not isinstance(raw, list):
        raise BoundaryLayerSmokeError("volume has no oriented boundary audit")
    result: list[int] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise BoundaryLayerSmokeError("oriented boundary record is invalid")
        tag = int(entry.get("surface_tag", 0))
        orientation = int(entry.get("orientation", 0))
        if tag <= 0 or orientation not in {-1, 1}:
            raise BoundaryLayerSmokeError("oriented boundary record is invalid")
        result.append(orientation * tag)
    return result


def _surface_faces_from_gmsh(gmsh: Any, surface_tags: Iterable[int]) -> list[list[int]]:
    result: dict[tuple[int, ...], list[int]] = {}
    for surface_tag in surface_tags:
        types, tags_by_type, nodes_by_type = gmsh.model.mesh.getElements(2, int(surface_tag))
        for raw_type, raw_tags, raw_nodes in zip(types, tags_by_type, nodes_by_type):
            properties = gmsh.model.mesh.getElementProperties(int(raw_type))
            name, order, node_count = str(properties[0]), int(properties[2]), int(properties[3])
            if name not in {"Triangle 3", "Quadrilateral 4"} or order != 1:
                raise BoundaryLayerSmokeError("surface mesh contains a non-linear face")
            element_tags = [int(value) for value in raw_tags]
            nodes = [int(value) for value in raw_nodes]
            if len(nodes) != len(element_tags) * node_count:
                raise BoundaryLayerSmokeError("surface connectivity is inconsistent")
            for index in range(len(element_tags)):
                face = nodes[index * node_count : (index + 1) * node_count]
                key = _face_key(face, f"surface {surface_tag} face")
                if key in result:
                    raise BoundaryLayerSmokeError("surface face is repeated across entities")
                result[key] = face
    return list(result.values())


def _volume_elements_from_gmsh(
    gmsh: Any, volume_regions: Mapping[int, str], *, max_elements: int
) -> list[dict[str, Any]]:
    """Extract linear cells and their official primary face nodes."""

    result: list[dict[str, Any]] = []
    seen_tags: set[int] = set()
    for volume_tag, region in volume_regions.items():
        types, tags_by_type, nodes_by_type = gmsh.model.mesh.getElements(3, int(volume_tag))
        for raw_type, raw_tags, raw_nodes in zip(types, tags_by_type, nodes_by_type):
            element_type = int(raw_type)
            properties = gmsh.model.mesh.getElementProperties(element_type)
            name, order, node_count = str(properties[0]), int(properties[2]), int(properties[3])
            if name not in _ALLOWED_3D_TYPES or order != 1:
                raise BoundaryLayerSmokeError("volume mesh contains a disallowed element type")
            element_tags = [int(value) for value in raw_tags]
            if len(result) + len(element_tags) > max_elements:
                raise BoundaryLayerSmokeError("3D element count exceeds the configured cap")
            nodes = [int(value) for value in raw_nodes]
            if len(nodes) != len(element_tags) * node_count:
                raise BoundaryLayerSmokeError("volume connectivity is inconsistent")
            faces_by_element: list[list[list[int]]] = [[] for _ in element_tags]
            for face_node_count in (3, 4):
                raw_faces = [
                    int(value)
                    for value in gmsh.model.mesh.getElementFaceNodes(
                        element_type,
                        face_node_count,
                        int(volume_tag),
                        primary=True,
                    )
                ]
                if not raw_faces:
                    continue
                denominator = len(element_tags) * face_node_count
                if denominator == 0 or len(raw_faces) % denominator:
                    raise BoundaryLayerSmokeError("official face connectivity is inconsistent")
                faces_per_element = len(raw_faces) // denominator
                for element_index in range(len(element_tags)):
                    for face_index in range(faces_per_element):
                        start = (
                            element_index * faces_per_element + face_index
                        ) * face_node_count
                        faces_by_element[element_index].append(
                            raw_faces[start : start + face_node_count]
                        )
            expected_faces = {"Tetrahedron 4": 4, "Prism 6": 5, "Pyramid 5": 5}[name]
            for index, element_tag in enumerate(element_tags):
                if element_tag in seen_tags or len(faces_by_element[index]) != expected_faces:
                    raise BoundaryLayerSmokeError("official element-face extraction is incomplete")
                seen_tags.add(element_tag)
                result.append(
                    {
                        "tag": element_tag,
                        "type": name,
                        "nodes": nodes[index * node_count : (index + 1) * node_count],
                        "faces": faces_by_element[index],
                        "region": str(region),
                    }
                )
    return result


def _quality_from_gmsh(gmsh: Any, element_tags: Sequence[int]) -> dict[int, dict[str, float]]:
    if not element_tags:
        raise BoundaryLayerSmokeError("cannot query quality for an empty mesh")
    result = {int(tag): {} for tag in element_tags}
    for metric in ("minDetJac", "minSJ", "minSICN", "volume"):
        values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(list(element_tags), metric)
        ]
        if len(values) != len(element_tags):
            raise BoundaryLayerSmokeError(f"Gmsh quality {metric} count is inconsistent")
        for tag, value in zip(element_tags, values):
            result[int(tag)][metric] = value
    return result


def _linear_quantile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise BoundaryLayerSmokeError("quality quantile requires at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise BoundaryLayerSmokeError("quality quantile fraction is outside [0, 1]")
    position = fraction * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(
        sorted_values[upper]
    ) * weight


def _quality_summary_for_entities(
    gmsh: Any,
    dimtags: Sequence[tuple[int, int]],
    *,
    element_name: str,
    threshold_metric: str | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    tags: list[int] = []
    for dimension, entity_tag in dimtags:
        if int(dimension) != 3:
            raise BoundaryLayerSmokeError("quality summary accepts only volume entities")
        types, tag_blocks, _ = gmsh.model.mesh.getElements(3, int(entity_tag))
        for raw_type, raw_tags in zip(types, tag_blocks):
            properties = gmsh.model.mesh.getElementProperties(int(raw_type))
            if str(properties[0]) == element_name:
                tags.extend(int(value) for value in raw_tags)
    if not tags:
        raise BoundaryLayerSmokeError(f"no {element_name} elements for quality summary")
    if (threshold_metric is None) != (threshold is None):
        raise BoundaryLayerSmokeError("quality threshold metric/value must be paired")
    threshold_value = (
        _finite(threshold, "quality threshold") if threshold is not None else None
    )
    if threshold_value is not None and threshold_value <= 0.0:
        raise BoundaryLayerSmokeError("quality threshold must be positive")
    quality_names = ("volume", "minDetJac", "minSJ", "minSICN", "gamma")
    if threshold_metric is not None and threshold_metric not in quality_names:
        raise BoundaryLayerSmokeError("quality threshold metric is unsupported")
    metrics: dict[str, dict[str, Any]] = {}
    bad_union: set[int] = set()
    below_threshold: list[int] = []
    for metric in quality_names:
        values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(tags, metric)
        ]
        if len(values) != len(tags):
            raise BoundaryLayerSmokeError(f"Gmsh quality {metric} count is inconsistent")
        bad = {
            tag
            for tag, value in zip(tags, values)
            if not math.isfinite(value) or value <= 0.0
        }
        bad_union.update(bad)
        ordered = sorted(values)
        metrics[metric] = {
            "minimum": ordered[0],
            "maximum": ordered[-1],
            "mean": sum(ordered) / len(ordered),
            "p001": _linear_quantile(ordered, 0.001),
            "p01": _linear_quantile(ordered, 0.01),
            "p05": _linear_quantile(ordered, 0.05),
            "p50": _linear_quantile(ordered, 0.50),
            "nonpositive_or_nonfinite_count": len(bad),
        }
        if metric == threshold_metric and threshold_value is not None:
            below_threshold = [
                tag
                for tag, value in zip(tags, values)
                if not math.isfinite(value) or value < threshold_value
            ]
    return {
        "element_type": element_name,
        "element_count": len(tags),
        "bad_element_count_union": len(bad_union),
        "bad_element_tags_audit": sorted(bad_union),
        "metrics": metrics,
        "threshold_metric": threshold_metric,
        "threshold": threshold_value,
        "below_threshold_count": len(below_threshold),
        "below_threshold_element_tags_audit": sorted(below_threshold),
    }


def _stable_coordinate_set_sha256(
    node_tags: Sequence[int], coordinates: Mapping[int, Sequence[float]]
) -> str:
    normalized = sorted(
        tuple(round(float(value), 14) for value in coordinates[int(tag)])
        for tag in node_tags
    )
    payload = json.dumps(normalized, separators=(",", ":"), allow_nan=False).encode(
        "ascii"
    )
    return hashlib.sha256(payload).hexdigest()


def _configure_quality_improvement_fields(
    gmsh: Any,
    resolved: Mapping[str, Any],
    base_refinement: Mapping[str, Any],
    quality_improvement: Mapping[str, Any],
    *,
    characteristic_length: float,
    configure_local_refinement: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Compose independent tag-free Threshold fields through a Gmsh Min field."""

    if quality_improvement.get("mode") != _QUALITY_IMPROVEMENT_MODE:
        raise BoundaryLayerSmokeError("quality-improvement field mode is invalid")
    raw_base_fingerprints = base_refinement.get("surface_fingerprint_ids")
    excluded = quality_improvement.get("base_excluded_surface_fingerprints")
    groups = quality_improvement.get("additional_refinement_groups")
    if (
        not isinstance(raw_base_fingerprints, list)
        or not isinstance(excluded, list)
        or not isinstance(groups, list)
        or not groups
    ):
        raise BoundaryLayerSmokeError("quality-improvement refinement inputs are incomplete")
    excluded_set = {str(value).casefold() for value in excluded}
    base_fingerprints = [
        str(value).casefold()
        for value in raw_base_fingerprints
        if str(value).casefold() not in excluded_set
    ]
    if not base_fingerprints or len(base_fingerprints) != len(set(base_fingerprints)):
        raise BoundaryLayerSmokeError(
            "quality-improvement base refinement became empty or ambiguous"
        )
    if not excluded_set <= {
        str(value).casefold() for value in raw_base_fingerprints
    }:
        raise BoundaryLayerSmokeError(
            "quality-improvement excludes a fingerprint absent from the base field"
        )
    base_contract = {
        **dict(base_refinement),
        "surface_fingerprint_ids": base_fingerprints,
    }
    audits: list[dict[str, Any]] = []
    base_audit = dict(
        configure_local_refinement(
            gmsh,
            resolved,
            base_contract,
            characteristic_length=characteristic_length,
        )
    )
    audits.append({"name": "topology_base_after_exclusions", **base_audit})
    threshold_fields = [int(base_audit["threshold_field_tag_audit"])]
    for raw_group in groups:
        if not isinstance(raw_group, Mapping):
            raise BoundaryLayerSmokeError("additional refinement group is invalid")
        group_contract = {
            "surface_fingerprint_ids": list(raw_group["surface_fingerprint_ids"]),
            "minimum_size_m": float(raw_group["minimum_size_m"]),
            "distance_max_m": float(raw_group["distance_max_m"]),
            "sampling": int(raw_group["sampling"]),
        }
        audit = dict(
            configure_local_refinement(
                gmsh,
                resolved,
                group_contract,
                characteristic_length=characteristic_length,
            )
        )
        audits.append({"name": str(raw_group["name"]), **audit})
        threshold_fields.append(int(audit["threshold_field_tag_audit"]))

    field_api = getattr(gmsh.model.mesh, "field", None)
    if field_api is None:
        raise BoundaryLayerSmokeError("Gmsh mesh field API is unavailable")
    minimum_field = int(field_api.add("Min"))
    if minimum_field <= 0:
        raise BoundaryLayerSmokeError("Gmsh Min field returned an invalid runtime tag")
    field_api.setNumbers(minimum_field, "FieldsList", threshold_fields)
    field_api.setAsBackgroundMesh(minimum_field)
    return {
        "schema": "cfdpipe.stable_surface_threshold_min.v1",
        "status": "PASS",
        "mode": _QUALITY_IMPROVEMENT_MODE,
        "runtime_tags_are_matching_criteria": False,
        "base_excluded_surface_fingerprints": sorted(excluded_set),
        "field_audits": audits,
        "threshold_field_tags_audit": threshold_fields,
        "minimum_field_tag_audit": minimum_field,
    }


def _core_tetra_quality_by_region(
    gmsh: Any,
    *,
    wall_adjacent_core_volume_tag: int,
    other_core_volume_tag: int,
    minimum_gamma: float,
) -> dict[str, Any]:
    threshold = _finite(minimum_gamma, "minimum core tetra gamma")
    if threshold <= 0.0 or threshold >= 1.0:
        raise BoundaryLayerSmokeError("minimum core tetra gamma must be in (0, 1)")
    coordinates = _nodes_from_gmsh(gmsh)
    result: dict[str, Any] = {
        "status": "PASS",
        "minimum_gamma_for_pass": threshold,
        "runtime_volume_tags_are_audit_only": True,
        "regions": {},
    }
    for role, entity_tag in (
        ("wall_adjacent_core", int(wall_adjacent_core_volume_tag)),
        ("other_core", int(other_core_volume_tag)),
    ):
        summary = _quality_summary_for_entities(
            gmsh,
            [(3, entity_tag)],
            element_name="Tetrahedron 4",
            threshold_metric="gamma",
            threshold=threshold,
        )
        records = {
            int(record["tag"]): record
            for record in _element_records_from_gmsh(gmsh, 3, entity_tag)
            if record["type"] == "Tetrahedron 4"
        }
        if len(records) != int(summary["element_count"]):
            raise BoundaryLayerSmokeError("core tetra quality records are incomplete")
        low_records: list[dict[str, Any]] = []
        low_tags = [
            int(value) for value in summary["below_threshold_element_tags_audit"]
        ]
        gamma_values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(low_tags, "gamma")
        ] if low_tags else []
        if len(gamma_values) != len(low_tags):
            raise BoundaryLayerSmokeError("low-gamma tetra quality records are incomplete")
        for tag, gamma in zip(low_tags, gamma_values):
            record = records[tag]
            nodes = [int(value) for value in record["nodes"]]
            centroid = [
                sum(float(coordinates[node][axis]) for node in nodes) / len(nodes)
                for axis in range(3)
            ]
            low_records.append(
                {
                    "element_tag_audit": tag,
                    "gamma": gamma,
                    "centroid_m": centroid,
                    "stable_node_coordinate_set_sha256": _stable_coordinate_set_sha256(
                        nodes, coordinates
                    ),
                }
            )
        result["regions"][role] = {
            **summary,
            "runtime_volume_tag_audit": entity_tag,
            "low_gamma_elements": low_records,
        }
    if any(
        int(record["bad_element_count_union"]) > 0
        for record in result["regions"].values()
    ):
        raise BoundaryLayerSmokeError("core tetra mesh contains nonpositive quality")
    result["below_gamma_count"] = sum(
        int(record["below_threshold_count"])
        for record in result["regions"].values()
    )
    result["status"] = "PASS" if result["below_gamma_count"] == 0 else "WARN"
    return result


def _prism_column_quality_summary(
    gmsh: Any,
    *,
    columns: Mapping[str, Sequence[Sequence[int]]],
    prism_base_faces: Mapping[str, Sequence[Sequence[int]]],
    minimum_scaled_jacobian: float,
) -> dict[str, Any]:
    threshold = _finite(
        minimum_scaled_jacobian, "minimum prism scaled Jacobian"
    )
    if threshold <= 0.0 or threshold >= 1.0:
        raise BoundaryLayerSmokeError("minimum prism scaled Jacobian must be in (0, 1)")
    if set(columns) != set(prism_base_faces):
        raise BoundaryLayerSmokeError("prism column/base-face keys are inconsistent")
    tags: list[int] = []
    owners: dict[int, tuple[str, int]] = {}
    for fingerprint in sorted(columns):
        local_columns = columns[fingerprint]
        local_faces = prism_base_faces[fingerprint]
        if len(local_columns) != len(local_faces):
            raise BoundaryLayerSmokeError("prism columns and base faces are not paired")
        for column_index, column in enumerate(local_columns):
            for raw_tag in column:
                tag = int(raw_tag)
                if tag in owners:
                    raise BoundaryLayerSmokeError("a prism belongs to multiple quality columns")
                owners[tag] = (str(fingerprint), column_index)
                tags.append(tag)
    if not tags:
        raise BoundaryLayerSmokeError("prism column quality requires prism elements")
    values = [
        float(value) for value in gmsh.model.mesh.getElementQualities(tags, "minSJ")
    ]
    if len(values) != len(tags) or any(not math.isfinite(value) for value in values):
        raise BoundaryLayerSmokeError("prism minSJ values are incomplete or non-finite")
    value_by_tag = dict(zip(tags, values))
    coordinates = _nodes_from_gmsh(gmsh)
    low_columns: list[dict[str, Any]] = []
    minimum = min(values)
    for fingerprint in sorted(columns):
        for index, column in enumerate(columns[fingerprint]):
            local_values = [value_by_tag[int(tag)] for tag in column]
            local_minimum = min(local_values)
            if local_minimum < threshold:
                base_face = [int(value) for value in prism_base_faces[fingerprint][index]]
                low_columns.append(
                    {
                        "wall_surface_fingerprint": str(fingerprint),
                        "column_index_audit": index,
                        "stable_base_coordinate_set_sha256": _stable_coordinate_set_sha256(
                            base_face, coordinates
                        ),
                        "base_face_node_tags_audit": base_face,
                        "element_tags_audit": [int(tag) for tag in column],
                        "layer_min_sj": local_values,
                        "minimum_min_sj": local_minimum,
                        "below_threshold_element_count": sum(
                            value < threshold for value in local_values
                        ),
                    }
                )
    ordered = sorted(values)
    return {
        "status": "PASS" if not low_columns else "WARN",
        "minimum_scaled_jacobian_for_pass": threshold,
        "element_count": len(tags),
        "column_count": sum(len(value) for value in columns.values()),
        "minimum_min_sj": minimum,
        "p001_min_sj": _linear_quantile(ordered, 0.001),
        "p01_min_sj": _linear_quantile(ordered, 0.01),
        "p05_min_sj": _linear_quantile(ordered, 0.05),
        "below_threshold_element_count": sum(value < threshold for value in values),
        "below_threshold_column_count": len(low_columns),
        "low_columns": low_columns,
    }


def _nodes_from_gmsh(gmsh: Any) -> dict[int, tuple[float, float, float]]:
    # With dim/tag set to -1, includeBoundary recursively revisits nodes on
    # shared lower-dimensional entities and can legitimately return duplicate
    # tags.  The no-argument whole-model form is the unique global node table.
    raw_tags, raw_coordinates, _ = gmsh.model.mesh.getNodes()
    tags = [int(value) for value in raw_tags]
    coordinates = [float(value) for value in raw_coordinates]
    if len(coordinates) != 3 * len(tags) or len(tags) != len(set(tags)):
        raise BoundaryLayerSmokeError("Gmsh node table is inconsistent")
    return {
        tag: tuple(coordinates[3 * index : 3 * index + 3])
        for index, tag in enumerate(tags)
    }


def _element_records_from_gmsh(
    gmsh: Any, dimension: int, entity_tag: int
) -> list[dict[str, Any]]:
    """Return linear mesh records while retaining the runtime element type."""

    result: list[dict[str, Any]] = []
    types, tags_by_type, nodes_by_type = gmsh.model.mesh.getElements(
        int(dimension), int(entity_tag)
    )
    for raw_type, raw_tags, raw_nodes in zip(types, tags_by_type, nodes_by_type):
        element_type = int(raw_type)
        properties = gmsh.model.mesh.getElementProperties(element_type)
        name, order, node_count = (
            str(properties[0]),
            int(properties[2]),
            int(properties[3]),
        )
        tags = [int(value) for value in raw_tags]
        nodes = [int(value) for value in raw_nodes]
        if order != 1 or len(nodes) != len(tags) * node_count:
            raise BoundaryLayerSmokeError("linear mesh element block is inconsistent")
        for index, tag in enumerate(tags):
            result.append(
                {
                    "tag": tag,
                    "element_type": element_type,
                    "type": name,
                    "nodes": tuple(
                        nodes[index * node_count : (index + 1) * node_count]
                    ),
                    "entity": int(entity_tag),
                }
            )
    return result


def _mesh_connectivity_sha256(gmsh: Any) -> str:
    records: list[tuple[int, int, tuple[int, ...]]] = []
    for dimension in (1, 2, 3):
        for record in _element_records_from_gmsh(gmsh, dimension, -1):
            records.append(
                (
                    int(record["tag"]),
                    int(record["element_type"]),
                    tuple(int(value) for value in record["nodes"]),
                )
            )
    payload = json.dumps(sorted(records), separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _strict_quality_summary(
    gmsh: Any, element_tags: Sequence[int]
) -> tuple[dict[str, Any], set[int]]:
    tags = [int(value) for value in element_tags]
    if not tags or len(tags) != len(set(tags)):
        raise BoundaryLayerSmokeError("quality summary requires unique element tags")
    metrics: dict[str, Any] = {}
    bad_union: set[int] = set()
    for metric in ("minDetJac", "minSJ", "minSICN", "volume"):
        values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(tags, metric)
        ]
        if len(values) != len(tags):
            raise BoundaryLayerSmokeError(f"Gmsh quality {metric} count is inconsistent")
        bad = {
            tag
            for tag, value in zip(tags, values)
            if not math.isfinite(value) or value <= 0.0
        }
        bad_union.update(bad)
        metrics[metric] = {
            "minimum": min(values),
            "maximum": max(values),
            "nonpositive_or_nonfinite_count": len(bad),
        }
    return (
        {
            "element_count": len(tags),
            "bad_element_count_union": len(bad_union),
            "bad_element_tags_audit": sorted(bad_union),
            "metrics": metrics,
        },
        bad_union,
    )


def _one_layer_prism_topology(
    gmsh: Any,
    *,
    prism_volume_tags: Sequence[int],
    wall_surface_tags: Sequence[int],
    top_surface_tags: Sequence[int],
    max_elements: int,
) -> dict[str, Any]:
    """Derive base/top pairing from official faces and lateral topology."""

    volume_regions = {
        int(tag): "boundary_layer" for tag in prism_volume_tags
    }
    official = _volume_elements_from_gmsh(
        gmsh, volume_regions, max_elements=max_elements
    )
    if not official or any(record["type"] != "Prism 6" for record in official):
        raise BoundaryLayerSmokeError("one-layer shell is not exclusively Prism 6")
    raw_by_tag: dict[int, dict[str, Any]] = {}
    for entity in prism_volume_tags:
        records = _element_records_from_gmsh(gmsh, 3, int(entity))
        if any(record["type"] != "Prism 6" for record in records):
            raise BoundaryLayerSmokeError("prism volume contains a non-Prism6 element")
        for record in records:
            tag = int(record["tag"])
            if tag in raw_by_tag:
                raise BoundaryLayerSmokeError("prism element tag is repeated")
            raw_by_tag[tag] = record
    if set(raw_by_tag) != {int(record["tag"]) for record in official}:
        raise BoundaryLayerSmokeError("official/raw Prism6 inventories differ")

    base_surface_faces = {
        _face_key(face, "wall surface face")
        for face in _surface_faces_from_gmsh(gmsh, wall_surface_tags)
    }
    top_surface_faces = {
        _face_key(face, "top surface face")
        for face in _surface_faces_from_gmsh(gmsh, top_surface_tags)
    }
    if not base_surface_faces or len(base_surface_faces) != len(top_surface_faces):
        raise BoundaryLayerSmokeError("one-layer wall/top face inventories differ")

    base_to_top_candidates: dict[int, set[int]] = defaultdict(set)
    prism_records: list[dict[str, Any]] = []
    for item in official:
        tag = int(item["tag"])
        triangles = [face for face in item["faces"] if len(face) == 3]
        quads = [face for face in item["faces"] if len(face) == 4]
        base_faces = [
            face
            for face in triangles
            if _face_key(face, f"prism {tag} base") in base_surface_faces
        ]
        top_faces = [
            face
            for face in triangles
            if _face_key(face, f"prism {tag} top") in top_surface_faces
        ]
        if len(base_faces) != 1 or len(top_faces) != 1 or len(quads) != 3:
            raise BoundaryLayerSmokeError(
                "Prism6 does not have one official wall base and one core top"
            )
        base_face = tuple(int(value) for value in base_faces[0])
        top_face = tuple(int(value) for value in top_faces[0])
        base_set, top_set = set(base_face), set(top_face)
        if base_set & top_set or set(item["nodes"]) != base_set | top_set:
            raise BoundaryLayerSmokeError("one-layer Prism6 node partition is invalid")
        for quad in quads:
            ordered = [int(value) for value in quad]
            for index, first in enumerate(ordered):
                second = ordered[(index + 1) % len(ordered)]
                if first in base_set and second in top_set:
                    base_to_top_candidates[first].add(second)
                elif second in base_set and first in top_set:
                    base_to_top_candidates[second].add(first)
        raw = raw_by_tag[tag]
        prism_records.append(
            {
                **raw,
                "faces": item["faces"],
                "base_face": base_face,
                "top_face": top_face,
            }
        )

    base_to_top = {
        base: next(iter(candidates))
        for base, candidates in base_to_top_candidates.items()
        if len(candidates) == 1
    }
    base_nodes = {node for record in prism_records for node in record["base_face"]}
    top_nodes = {node for record in prism_records for node in record["top_face"]}
    if (
        set(base_to_top) != base_nodes
        or set(base_to_top.values()) != top_nodes
        or len(base_to_top.values()) != len(set(base_to_top.values()))
    ):
        raise BoundaryLayerSmokeError("official lateral faces do not define a bijective base/top map")
    inverse_top = {top: base for base, top in base_to_top.items()}
    for record in prism_records:
        roots = {
            node if node in base_to_top else inverse_top[node]
            for node in record["nodes"]
        }
        if roots != set(record["base_face"]):
            raise BoundaryLayerSmokeError("raw Prism6 ordering disagrees with official topology")
    return {
        "records": prism_records,
        "base_to_top": base_to_top,
        "inverse_top": inverse_top,
        "base_nodes": base_nodes,
        "top_nodes": top_nodes,
        "base_face_keys": base_surface_faces,
        "top_face_keys": top_surface_faces,
    }


def _derive_prism_columns(
    elements: Sequence[Mapping[str, Any]],
    wall_faces_by_fingerprint: Mapping[str, Sequence[Sequence[int]]],
    collar_base_faces: Mapping[str, Sequence[Sequence[int]]],
) -> tuple[dict[str, list[list[int]]], dict[str, list[list[int]]]]:
    prism_triangles: dict[int, set[tuple[int, ...]]] = {}
    triangle_owners: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for element in elements:
        if element.get("type") != "Prism 6":
            continue
        tag = int(element["tag"])
        triangles = {
            _face_key(face, f"prism {tag} face")
            for face in element.get("faces", [])
            if len(face) == 3
        }
        if len(triangles) != 2:
            raise BoundaryLayerSmokeError("a Prism 6 does not expose two official triangle faces")
        prism_triangles[tag] = triangles
        for face in triangles:
            triangle_owners[face].append(tag)
    columns: dict[str, list[list[int]]] = {}
    covered: dict[str, list[list[int]]] = {}
    for fingerprint, raw_faces in wall_faces_by_fingerprint.items():
        local: list[list[int]] = []
        local_covered: list[list[int]] = []
        approved_collar = {
            _face_key(face, f"wall {fingerprint} approved collar base")
            for face in collar_base_faces.get(fingerprint, [])
        }
        for raw_face in raw_faces:
            base = _face_key(raw_face, f"wall {fingerprint} base face")
            owners = triangle_owners.get(base, [])
            if not owners and base in approved_collar:
                continue
            if len(owners) != 1:
                raise BoundaryLayerSmokeError("wall base face has no unique first prism")
            previous_face = base
            current = owners[0]
            column: list[int] = []
            while True:
                if current in column:
                    raise BoundaryLayerSmokeError("prism column contains a cycle")
                column.append(current)
                continuation: list[tuple[tuple[int, ...], int]] = []
                for face in prism_triangles[current] - {previous_face}:
                    other = [tag for tag in triangle_owners.get(face, []) if tag != current]
                    if len(other) > 1:
                        raise BoundaryLayerSmokeError("prism triangle is non-manifold")
                    if other:
                        continuation.append((face, other[0]))
                if not continuation:
                    break
                if len(continuation) != 1:
                    raise BoundaryLayerSmokeError("prism column branches")
                previous_face, current = continuation[0]
            local.append(column)
            local_covered.append(list(raw_face))
        columns[fingerprint] = local
        covered[fingerprint] = local_covered
    return columns, covered


def _physical_group_inventory(gmsh: Any) -> dict[tuple[int, str], list[int]]:
    result: dict[tuple[int, str], list[int]] = {}
    for raw_dim, raw_tag in gmsh.model.getPhysicalGroups():
        dimension, physical_tag = int(raw_dim), int(raw_tag)
        name = str(gmsh.model.getPhysicalName(dimension, physical_tag)).strip()
        if not name or (dimension, name) in result:
            raise BoundaryLayerSmokeError("physical group names are empty or duplicated")
        result[(dimension, name)] = sorted(
            int(value)
            for value in gmsh.model.getEntitiesForPhysicalGroup(dimension, physical_tag)
        )
    return result


def _audit_wall_inward_direction(
    gmsh: Any,
    catalog: Mapping[str, Any],
    surface_tag: int,
    *,
    probe_distances_m: Sequence[float],
) -> dict[str, Any]:
    """Prove one wall-normal sign with trimmed-surface bilateral samples.

    Fixed parametric fractions are not sufficient for the project's narrow
    trimmed BSpline patches.  Candidate points are first filtered by the
    actual trim, then both normal directions are tested at progressively larger
    hash-bound geometry tolerances.  Runtime OCC orientation is evidence only.
    """

    distances = sorted(
        {
            _finite(value, "direction probe distance")
            for value in probe_distances_m
            if _finite(value, "direction probe distance") > 0.0
        }
    )
    if not distances:
        raise BoundaryLayerSmokeError("direction probe distances are empty")
    surfaces = catalog.get("surfaces")
    volumes = catalog.get("volumes")
    if not isinstance(surfaces, list) or not isinstance(volumes, list):
        raise BoundaryLayerSmokeError("fresh geometry catalog is incomplete")
    surface_matches = [
        record
        for record in surfaces
        if isinstance(record, Mapping)
        and int(record.get("entity_tag", -1)) == int(surface_tag)
    ]
    if len(surface_matches) != 1:
        raise BoundaryLayerSmokeError("wall direction surface is not unique")
    adjacent = surface_matches[0].get("adjacent_volumes")
    if not isinstance(adjacent, list) or len(adjacent) != 1:
        raise BoundaryLayerSmokeError("wall direction surface is not external")
    volume_tag = int(adjacent[0])
    volume_matches = [
        record
        for record in volumes
        if isinstance(record, Mapping)
        and int(record.get("entity_tag", -1)) == volume_tag
    ]
    if len(volume_matches) != 1:
        raise BoundaryLayerSmokeError("wall adjacent volume is not unique")
    oriented = volume_matches[0].get("oriented_boundary_surfaces")
    orientation_matches = [
        record
        for record in oriented or []
        if isinstance(record, Mapping)
        and int(record.get("surface_tag", -1)) == int(surface_tag)
    ]
    if len(orientation_matches) != 1:
        raise BoundaryLayerSmokeError("wall has no unique orientation audit")
    orientation = int(orientation_matches[0].get("orientation", 0))
    if orientation not in {-1, 1}:
        raise BoundaryLayerSmokeError("wall orientation audit is invalid")

    minimum_raw, maximum_raw = gmsh.model.getParametrizationBounds(2, surface_tag)
    minimum = [_finite(value, "surface parameter minimum") for value in minimum_raw]
    maximum = [_finite(value, "surface parameter maximum") for value in maximum_raw]
    if len(minimum) != 2 or len(maximum) != 2:
        raise BoundaryLayerSmokeError("wall parametrization bounds are invalid")
    preferred = [
        (0.50, 0.50, "parametric_2_2"),
        (0.25, 0.25, "parametric_1_1"),
        (0.75, 0.75, "parametric_3_3"),
        (0.25, 0.75, "parametric_1_3"),
        (0.75, 0.25, "parametric_3_1"),
    ]
    dense = [
        (u / 10.0, v / 10.0, f"dense_{u}_{v}")
        for u in range(1, 10)
        for v in range(1, 10)
    ]
    preferred_pairs = {(value[0], value[1]) for value in preferred}
    candidates = preferred + [
        value for value in dense if (value[0], value[1]) not in preferred_pairs
    ]
    samples: list[dict[str, Any]] = []
    for u_fraction, v_fraction, label in candidates:
        parameters = [
            minimum[0] + u_fraction * (maximum[0] - minimum[0]),
            minimum[1] + v_fraction * (maximum[1] - minimum[1]),
        ]
        if int(gmsh.model.isInside(2, surface_tag, parameters, True)) <= 0:
            continue
        point = [
            _finite(value, "surface sample coordinate")
            for value in gmsh.model.getValue(2, surface_tag, parameters)
        ]
        normal = [
            _finite(value, "surface sample normal")
            for value in gmsh.model.getNormal(surface_tag, parameters)
        ]
        magnitude = math.sqrt(sum(value * value for value in normal))
        if len(point) != 3 or len(normal) != 3 or magnitude <= 0.0:
            raise BoundaryLayerSmokeError("wall direction sample is invalid")
        unit = [value / magnitude for value in normal]
        tests: list[dict[str, Any]] = []
        resolved_sign: int | None = None
        for distance in distances:
            plus = [point[index] + distance * unit[index] for index in range(3)]
            minus = [point[index] - distance * unit[index] for index in range(3)]
            plus_inside = int(gmsh.model.isInside(3, volume_tag, plus))
            minus_inside = int(gmsh.model.isInside(3, volume_tag, minus))
            tests.append(
                {
                    "epsilon_m": distance,
                    "plus_inside_count": plus_inside,
                    "minus_inside_count": minus_inside,
                }
            )
            if (plus_inside > 0) != (minus_inside > 0):
                resolved_sign = 1 if plus_inside > 0 else -1
                break
        if resolved_sign is None:
            continue
        samples.append(
            {
                "label": label,
                "parametric_coordinates": parameters,
                "point_m": point,
                "surface_unit_normal": unit,
                "tests": tests,
                "resolved_height_sign": resolved_sign,
            }
        )
        if len(samples) == 5:
            break
    signs = {int(sample["resolved_height_sign"]) for sample in samples}
    if len(samples) != 5 or len(signs) != 1:
        raise BoundaryLayerSmokeError(
            "trimmed-surface bilateral audit did not resolve five consistent samples"
        )
    sign = next(iter(signs))
    return {
        "surface_tag_audit": int(surface_tag),
        "adjacent_volume_tag_audit": volume_tag,
        "oriented_boundary_sign_audit": orientation,
        "oriented_boundary_sign_used_for_direction": False,
        "inward_height_sign": sign,
        "method": "trim-filtered multi-distance bilateral gmsh.model.isInside",
        "probe_distance_candidates_m": distances,
        "samples": samples,
        "status": "PASS",
    }


def _repair_layer_schedule(config: Mapping[str, Any]) -> list[float]:
    first = _finite(config.get("first_layer_height_m"), "first layer height")
    growth = _finite(config.get("growth_ratio"), "growth ratio")
    layers = _positive_int(config.get("layer_count"), "layer count")
    if first <= 0.0 or growth <= 1.0 or layers < _REQUIRED_LAYER_COUNT:
        raise BoundaryLayerSmokeError("post-mesh repair layer schedule is invalid")
    result: list[float] = []
    running = 0.0
    for index in range(layers):
        running += first * growth**index
        result.append(running)
    return result


def _validated_direction_replay_approval_for_config(
    config: Mapping[str, Any],
    replay_approval: Mapping[str, Any],
    *,
    local_schedule_binding_sha256: str,
) -> dict[str, Any]:
    """Validate one replay approval against the current audit configuration."""

    try:
        source_quality = replay_approval["source_minimum_growth_final_quality"]
        return validate_direction_replay_approval(
            replay_approval,
            expected_coarse_contract_sha256=str(
                config.get("coarse_contract_sha256", "")
            ),
            expected_characteristic_length_m=float(
                config.get("characteristic_length_m")
            ),
            expected_local_schedule_binding_sha256=(
                local_schedule_binding_sha256.casefold()
            ),
            expected_projection_manifest_sha256=str(
                replay_approval.get("projection_manifest_sha256", "")
            ),
            expected_projected_3d_elements=(
                int(source_quality["prism_element_count"])
                + int(source_quality["core_element_count"])
            ),
            expected_minimum_prism_scaled_jacobian=float(
                config["quality_improvement"]["minimum_prism_scaled_jacobian"]
            ),
            expected_minimum_core_tetra_gamma=float(
                config["quality_improvement"]["minimum_core_tetra_gamma"]
            ),
        )
    except (
        CoarseDirectionReplayError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise BoundaryLayerSmokeError(
            f"physical-schedule direction replay approval is invalid: {error}"
        ) from error


def _expected_repair_audit_schema(config: Mapping[str, Any]) -> str:
    """Select the post-hook schema without conflating replay and homotopy."""

    if config.get("schedule_direction_continuation_endpoint") is not None:
        return SCHEDULE_DIRECTION_CONTINUATION_DISCOVERY_SCHEMA
    if config.get("physical_schedule_homotopy_endpoint") is not None:
        return HOMOTOPY_DISCOVERY_SCHEMA
    if config.get("owner_free_direction_replay_approval") is not None:
        return "cfdpipe.coarse_physical_schedule_fixed_direction_discovery.v1"
    if config.get("owner_free_direction_endpoint") is not None:
        return COMPONENT_DIRECTION_DISCOVERY_SCHEMA
    return "cfdpipe.coarse_repair_discovery.v1"


def _validated_local_surface_schedules(
    config: Mapping[str, Any],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Return hash-checked physical schedules keyed by stable wall identity.

    Coarse calibration is not allowed to silently fall back to the uniform
    design stack.  Projection-only exits before this function is reached.
    """

    contract_mode = config.get("contract_mode", "smoke")
    if contract_mode == "smoke":
        if (
            config.get("smoke_only") is not True
            or config.get("production_mesh_eligible") is not False
            or config.get("su2_called") is not False
            or config.get("paraview_called") is not False
            or config.get("local_boundary_layer_schedule") is not None
        ):
            raise BoundaryLayerSmokeError(
                "uniform smoke schedule violates the complete smoke safety scope"
            )
        fingerprints = {
            str(value).casefold()
            for value in config.get("wall_surface_fingerprints", [])
        }
        if len(fingerprints) != _REQUIRED_WALL_MEMBER_COUNT:
            raise BoundaryLayerSmokeError(
                "uniform smoke schedule does not cover all 48 stable walls"
            )
        cumulative = _repair_layer_schedule(config)
        schedules = {
            fingerprint: list(cumulative) for fingerprint in sorted(fingerprints)
        }
        unsigned_evidence = {
            "schema": "cfdpipe.uniform_boundary_layer_schedule_binding.v1",
            "status": "PASS",
            "schedule_mode": "uniform_design_stack",
            "surface_count": len(schedules),
            "layer_count": len(cumulative),
            "first_layer_height_m": cumulative[0],
            "minimum_surface_total_thickness_m": cumulative[-1],
            "maximum_surface_total_thickness_m": cumulative[-1],
            "runtime_entity_tags_present_in_binding": False,
        }
        return schedules, {
            **unsigned_evidence,
            "binding_sha256": _canonical_hash(unsigned_evidence),
        }
    mode_matrix = {
        "coarse_calibration": {
            "smoke_only": False,
            "calibration_only": True,
            "repair_audit_only": False,
            "projection_only": False,
            "production_mesh_eligible": False,
            "su2_called": False,
            "paraview_called": False,
        },
        "coarse_repair_audit_only": {
            "smoke_only": False,
            "calibration_only": False,
            "repair_audit_only": True,
            "projection_only": False,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
        },
    }
    expected_mode_scope = mode_matrix.get(contract_mode)
    if expected_mode_scope is None:
        raise BoundaryLayerSmokeError(
            "local surface schedules require coarse calibration or the complete "
            "audit-only repair safety scope"
        )
    if any(
        config.get(name) is not expected
        for name, expected in expected_mode_scope.items()
    ):
        scope_name = (
            "audit-only repair safety scope"
            if contract_mode == "coarse_repair_audit_only"
            else "coarse calibration mode scope"
        )
        raise BoundaryLayerSmokeError(
            f"local surface schedules violate the complete {scope_name}"
        )
    try:
        raw_binding = validate_boundary_layer_schedule_binding_for_strategy(
            config, config.get("local_boundary_layer_schedule")
        )
    except BoundaryLayerScheduleBindingError as error:
        raise BoundaryLayerSmokeError(
            f"local boundary-layer schedule failed final consumption validation: {error}"
        ) from error
    if not isinstance(raw_binding, Mapping):
        raise BoundaryLayerSmokeError(
            "coarse calibration requires a hash-bound local boundary-layer schedule"
        )
    if (
        raw_binding.get("schema")
        != "cfdpipe.boundary_layer_local_schedule_binding.v1"
        or raw_binding.get("status") != "PASS"
    ):
        raise BoundaryLayerSmokeError("local boundary-layer schedule binding is not PASS")
    unsigned_binding = dict(raw_binding)
    configured_binding_hash = unsigned_binding.pop("binding_sha256", None)
    if (
        not isinstance(configured_binding_hash, str)
        or _SHA256.fullmatch(configured_binding_hash.casefold()) is None
        or _canonical_hash(unsigned_binding) != configured_binding_hash.casefold()
    ):
        raise BoundaryLayerSmokeError("local boundary-layer schedule binding hash is stale")
    layer_count = _positive_int(config.get("layer_count"), "layer count")
    first_height = _finite(config.get("first_layer_height_m"), "first layer height")
    if (
        layer_count < _REQUIRED_LAYER_COUNT
        or raw_binding.get("layer_count") != layer_count
        or _positive_int(
            raw_binding.get("minimum_layer_count"), "bound minimum layer count"
        )
        < _REQUIRED_LAYER_COUNT
        or not math.isclose(
            _finite(raw_binding.get("first_layer_height_m"), "bound first height"),
            first_height,
            rel_tol=1.0e-11,
            abs_tol=1.0e-14,
        )
    ):
        raise BoundaryLayerSmokeError(
            "local boundary-layer schedule violates the fixed layer invariants"
        )
    expected_fingerprints = {
        str(value).casefold() for value in config.get("wall_surface_fingerprints", [])
    }
    raw_schedules = raw_binding.get("surface_schedules")
    if (
        len(expected_fingerprints) != 48
        or not isinstance(raw_schedules, Mapping)
        or {str(value).casefold() for value in raw_schedules} != expected_fingerprints
    ):
        raise BoundaryLayerSmokeError(
            "local boundary-layer schedule does not cover all 48 stable walls"
        )
    schedules: dict[str, list[float]] = {}
    totals: list[float] = []
    for raw_fingerprint, raw_record in raw_schedules.items():
        fingerprint = str(raw_fingerprint).casefold()
        if not isinstance(raw_record, Mapping) or (
            str(raw_record.get("surface_fingerprint_id", "")).casefold()
            != fingerprint
        ):
            raise BoundaryLayerSmokeError("local surface schedule identity is invalid")
        raw_cumulative = raw_record.get("cumulative_heights_m")
        if not isinstance(raw_cumulative, list) or len(raw_cumulative) != layer_count:
            raise BoundaryLayerSmokeError("local cumulative schedule is incomplete")
        cumulative = [
            _finite(value, "local cumulative height") for value in raw_cumulative
        ]
        if (
            any(value <= 0.0 for value in cumulative)
            or any(right <= left for left, right in zip(cumulative, cumulative[1:]))
            or not math.isclose(
                cumulative[0], first_height, rel_tol=1.0e-11, abs_tol=1.0e-14
            )
            or not math.isclose(
                cumulative[-1],
                _finite(raw_record.get("total_thickness_m"), "local total height"),
                rel_tol=1.0e-11,
                abs_tol=1.0e-14,
            )
        ):
            raise BoundaryLayerSmokeError(
                "local cumulative schedule is non-finite, non-monotone, or stale"
            )
        schedules[fingerprint] = cumulative
        totals.append(cumulative[-1])
    schedule_evidence: dict[str, Any] = {
        "binding_sha256": configured_binding_hash.casefold(),
        "source_plan_sha256": str(raw_binding.get("source_plan_sha256", "")).casefold(),
        "surface_count": len(schedules),
        "limited_surface_count": int(raw_binding.get("limited_surface_count", 0)),
        "minimum_surface_total_thickness_m": min(totals),
        "maximum_surface_total_thickness_m": max(totals),
        "runtime_entity_tags_present_in_binding": raw_binding.get(
            "runtime_entity_tags_present"
        ),
    }
    replay_approval = config.get("owner_free_direction_replay_approval")
    endpoint = config.get("schedule_feasibility_endpoint")
    continuation_endpoint = config.get(
        "schedule_direction_continuation_endpoint"
    )
    if (
        isinstance(endpoint, Mapping)
        and endpoint.get("schema") == HOMOTOPY_ENDPOINT_SCHEMA
    ):
        continuation_requested = continuation_endpoint is not None
        expected_homotopy_purpose = (
            "physical_schedule_first_frontier_direction_continuation"
            if continuation_requested
            else "physical_local_schedule_fixed_direction_homotopy"
        )
        if (
            not isinstance(replay_approval, Mapping)
            or contract_mode != "coarse_repair_audit_only"
            or config.get("audit_purpose")
            != expected_homotopy_purpose
            or config.get("fixed_direction_replay_only") is not True
            or config.get("combined_endpoint_only") is not True
            or (
                continuation_requested
                and config.get("first_frontier_direction_continuation_only")
                is not True
            )
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy scope or replay approval is incomplete"
            )
        replay_binding_hash = configured_binding_hash
        if continuation_requested:
            continuation_source_bindings = (
                continuation_endpoint.get("source_bindings", {})
                if isinstance(continuation_endpoint, Mapping)
                else {}
            )
            replay_binding_hash = str(
                continuation_source_bindings.get(
                    "local_schedule_binding_sha256", ""
                )
            )
        validated_replay = _validated_direction_replay_approval_for_config(
            config,
            replay_approval,
            local_schedule_binding_sha256=replay_binding_hash,
        )
        try:
            validated_homotopy = validate_physical_schedule_homotopy_endpoint(
                endpoint,
                expected_binding_sha256=replay_binding_hash,
                expected_first_layer_height_m=first_height,
                expected_layer_count=layer_count,
            )
            minimum_schedules, minimum_application = (
                interpolate_physical_schedule_homotopy(
                    schedules,
                    validated_homotopy,
                    0.0,
                    expected_surface_fingerprints=sorted(expected_fingerprints),
                )
            )
        except CoarseDirectionReplayError as error:
            raise BoundaryLayerSmokeError(
                f"physical-schedule homotopy endpoint is invalid: {error}"
            ) from error
        validated_continuation: dict[str, Any] | None = None
        if continuation_requested:
            if (
                not isinstance(continuation_endpoint, Mapping)
                or continuation_endpoint.get("schema")
                != SCHEDULE_DIRECTION_CONTINUATION_ENDPOINT_SCHEMA
            ):
                raise BoundaryLayerSmokeError(
                    "physical-schedule frontier endpoint is missing"
                )
            try:
                source_bindings = continuation_endpoint["source_bindings"]
                validated_continuation = validate_frontier_direction_endpoint(
                    continuation_endpoint,
                    homotopy_manifest_sha256=str(
                        source_bindings["homotopy_manifest_sha256"]
                    ),
                    homotopy_discovery_sha256=str(
                        source_bindings["homotopy_discovery_sha256"]
                    ),
                    homotopy_endpoint_sha256=str(
                        source_bindings["homotopy_endpoint_sha256"]
                    ),
                    direction_replay_approval_sha256=str(
                        source_bindings[
                            "direction_replay_approval_sha256"
                        ]
                    ),
                    local_schedule_binding_sha256=str(
                        source_bindings["local_schedule_binding_sha256"]
                    ),
                    previous_quality_sha256=str(
                        continuation_endpoint["previous_quality_sha256"]
                    ),
                    target_quality_sha256=str(
                        continuation_endpoint["target_quality_sha256"]
                    ),
                    target_low_quality_aggregate_sha256=str(
                        continuation_endpoint[
                            "target_low_quality_aggregate_sha256"
                        ]
                    ),
                    first_layer_height_m=first_height,
                    layer_count=layer_count,
                    target_observation=continuation_endpoint[
                        "target_observation"
                    ],
                )
            except (
                CoarseScheduleContinuationError,
                KeyError,
                TypeError,
            ) as error:
                raise BoundaryLayerSmokeError(
                    f"physical-schedule frontier endpoint is invalid: {error}"
                ) from error
            # The controller has already bridged the frozen Windows evidence
            # to the validated portable runtime objects before this strategy
            # is invoked.  Requiring their path-derived object hashes to be
            # identical here would reject that attested cross-platform bridge;
            # each endpoint remains independently self-hashed and strict.
        schedule_evidence.update(
            {
                "minimum_growth_schedule_used": True,
                "baseline_surface_schedules_sha256": _canonical_hash(
                    {"surface_schedules": schedules}
                ),
                "baseline_minimum_surface_total_thickness_m": min(totals),
                "baseline_maximum_surface_total_thickness_m": max(totals),
                "minimum_surface_total_thickness_m": min(
                    values[-1] for values in minimum_schedules.values()
                ),
                "maximum_surface_total_thickness_m": max(
                    values[-1] for values in minimum_schedules.values()
                ),
                "authoritative_physical_surface_schedules": schedules,
                "physical_schedule_homotopy_endpoint": validated_homotopy,
                "schedule_feasibility_endpoint": validated_homotopy,
                "schedule_feasibility_application": minimum_application,
                "owner_free_direction_replay_approval_sha256": (
                    validated_replay["approval_sha256"]
                ),
                "owner_free_direction_replay_approval": validated_replay,
            }
        )
        if validated_continuation is not None:
            schedule_evidence[
                "schedule_direction_continuation_endpoint"
            ] = validated_continuation
        return minimum_schedules, schedule_evidence
    if endpoint is None:
        if replay_approval is not None:
            if (
                contract_mode != "coarse_repair_audit_only"
                or config.get("audit_purpose")
                != "physical_local_schedule_fixed_direction_replay"
                or config.get("fixed_direction_replay_only") is not True
                or config.get("combined_endpoint_only") is not False
            ):
                raise BoundaryLayerSmokeError(
                    "physical-schedule direction replay scope is incomplete"
                )
            validated_replay = _validated_direction_replay_approval_for_config(
                config,
                replay_approval,
                local_schedule_binding_sha256=configured_binding_hash,
            )
            schedule_evidence.update(
                {
                    "minimum_growth_schedule_used": False,
                    "baseline_surface_schedules_sha256": _canonical_hash(
                        {"surface_schedules": schedules}
                    ),
                    "owner_free_direction_replay_approval_sha256": (
                        validated_replay["approval_sha256"]
                    ),
                    "owner_free_direction_replay_approval": validated_replay,
                }
            )
        return schedules, schedule_evidence
    if replay_approval is not None:
        raise BoundaryLayerSmokeError(
            "fixed direction replay cannot also apply a minimum-growth endpoint"
        )
    if contract_mode != "coarse_repair_audit_only":
        raise BoundaryLayerSmokeError(
            "schedule feasibility endpoint is restricted to repair audit mode"
        )
    try:
        validated_endpoint = validate_minimum_growth_endpoint(
            endpoint,
            expected_binding_sha256=configured_binding_hash,
            expected_first_layer_height_m=first_height,
            expected_layer_count=layer_count,
        )
        candidate_schedules, endpoint_evidence = apply_minimum_growth_endpoint(
            schedules,
            validated_endpoint,
            expected_surface_fingerprints=sorted(expected_fingerprints),
        )
    except CoarseScheduleFeasibilityError as error:
        raise BoundaryLayerSmokeError(
            f"schedule feasibility endpoint is invalid: {error}"
        ) from error
    direction_endpoint = config.get("owner_free_direction_endpoint")
    expected_purpose = (
        "minimum_growth_plus_existing_direction_repair"
        if direction_endpoint is None
        else (
            "minimum_growth_owner_free_component_pattern_audit"
            if isinstance(direction_endpoint, Mapping)
            and direction_endpoint.get("schema") == PATTERN_ENDPOINT_SCHEMA
            else "minimum_growth_owner_free_component_direction_audit"
        )
    )
    if (
        config.get("audit_purpose") != expected_purpose
        or config.get("combined_endpoint_only") is not True
    ):
        raise BoundaryLayerSmokeError(
            "schedule feasibility endpoint purpose/scope is incomplete"
        )
    if direction_endpoint is not None:
        try:
            validated_direction_endpoint = (
                validate_owner_free_direction_endpoint(
                    direction_endpoint,
                    expected_schedule_endpoint_sha256=str(
                        validated_endpoint["endpoint_sha256"]
                    ),
                )
            )
        except CoarseDirectionFeasibilityError as error:
            raise BoundaryLayerSmokeError(
                f"owner-free direction endpoint is invalid: {error}"
            ) from error
        schedule_evidence["owner_free_direction_endpoint"] = (
            validated_direction_endpoint
        )
    schedule_evidence["baseline_minimum_surface_total_thickness_m"] = (
        schedule_evidence.pop("minimum_surface_total_thickness_m")
    )
    schedule_evidence["baseline_maximum_surface_total_thickness_m"] = (
        schedule_evidence.pop("maximum_surface_total_thickness_m")
    )
    schedule_evidence["minimum_surface_total_thickness_m"] = min(
        values[-1] for values in candidate_schedules.values()
    )
    schedule_evidence["maximum_surface_total_thickness_m"] = max(
        values[-1] for values in candidate_schedules.values()
    )
    schedule_evidence["schedule_feasibility_endpoint"] = validated_endpoint
    schedule_evidence["schedule_feasibility_application"] = endpoint_evidence
    return candidate_schedules, schedule_evidence


def _assign_root_local_schedules(
    source_prisms: Sequence[Mapping[str, Any]],
    *,
    prism_volume_fingerprints: Mapping[int, str],
    surface_schedules: Mapping[str, Sequence[float]],
    root_coordinates: Mapping[int, Sequence[float]] | None = None,
) -> tuple[dict[int, list[float]], dict[str, Any]]:
    """Conservatively assign one physical schedule to every prism root.

    A shared edge/corner root takes the element-wise minimum of every incident
    stable surface schedule.  Consequently all prisms sharing that root use
    exactly one chain, remain continuous, and cannot enlarge any input plan.
    """

    normalized_surfaces: dict[str, list[float]] = {}
    layer_count: int | None = None
    common_first: float | None = None
    for raw_fingerprint, raw_values in surface_schedules.items():
        fingerprint = str(raw_fingerprint).casefold()
        values = [_finite(value, "surface cumulative height") for value in raw_values]
        if not values or any(value <= 0.0 for value in values) or any(
            right <= left for left, right in zip(values, values[1:])
        ):
            raise BoundaryLayerSmokeError("surface cumulative schedule is invalid")
        if layer_count is None:
            layer_count = len(values)
            common_first = values[0]
        if (
            len(values) != layer_count
            or not math.isclose(
                values[0], float(common_first), rel_tol=1.0e-11, abs_tol=1.0e-14
            )
        ):
            raise BoundaryLayerSmokeError(
                "surface schedules do not share one first height and layer count"
            )
        normalized_surfaces[fingerprint] = values
    if layer_count is None or layer_count < _REQUIRED_LAYER_COUNT:
        raise BoundaryLayerSmokeError("local schedules contain fewer than 15 layers")

    entity_fingerprints = {
        int(entity): str(fingerprint).casefold()
        for entity, fingerprint in prism_volume_fingerprints.items()
    }
    incident: dict[int, set[str]] = defaultdict(set)
    observed_surfaces: set[str] = set()
    for record in source_prisms:
        entity = int(record.get("entity", -1))
        fingerprint = entity_fingerprints.get(entity)
        if fingerprint is None or fingerprint not in normalized_surfaces:
            raise BoundaryLayerSmokeError(
                "one prism has no stable, scheduled wall-surface owner"
            )
        roots = tuple(int(value) for value in record.get("base_face", ()))
        if len(roots) != 3 or len(set(roots)) != 3:
            raise BoundaryLayerSmokeError("one source prism has an invalid base triangle")
        observed_surfaces.add(fingerprint)
        for root in roots:
            incident[root].add(fingerprint)
    if not incident:
        raise BoundaryLayerSmokeError("local schedule assignment found no prism roots")
    if observed_surfaces != set(normalized_surfaces):
        missing = sorted(set(normalized_surfaces) - observed_surfaces)
        raise BoundaryLayerSmokeError(
            "one or more scheduled stable wall surfaces generated no source prisms: "
            + repr(missing)
        )

    root_schedules: dict[int, list[float]] = {}
    per_surface_root_counts: Counter[str] = Counter()
    shared_root_count = 0
    limited_root_count = 0
    maximum_surface_total = max(values[-1] for values in normalized_surfaces.values())
    root_records: list[dict[str, Any]] = []
    for root in sorted(incident):
        fingerprints = sorted(incident[root])
        for fingerprint in fingerprints:
            per_surface_root_counts[fingerprint] += 1
        selected = [
            min(normalized_surfaces[fingerprint][index] for fingerprint in fingerprints)
            for index in range(layer_count)
        ]
        if any(right <= left for left, right in zip(selected, selected[1:])):
            raise BoundaryLayerSmokeError(
                "element-wise minimum root schedule is not strictly increasing"
            )
        for fingerprint in fingerprints:
            if any(
                selected[index] > normalized_surfaces[fingerprint][index]
                and not math.isclose(
                    selected[index],
                    normalized_surfaces[fingerprint][index],
                    rel_tol=1.0e-12,
                    abs_tol=1.0e-15,
                )
                for index in range(layer_count)
            ):
                raise BoundaryLayerSmokeError(
                    "root schedule amplified an incident surface plan"
                )
        root_schedules[root] = selected
        shared_root_count += int(len(fingerprints) > 1)
        limited = selected[-1] < maximum_surface_total and not math.isclose(
            selected[-1], maximum_surface_total, rel_tol=1.0e-12, abs_tol=1.0e-15
        )
        limited_root_count += int(limited)
        record: dict[str, Any] = {
            "root_tag_audit": root,
            "incident_wall_surface_fingerprints": fingerprints,
            "incident_surface_count": len(fingerprints),
            "selected_total_thickness_m": selected[-1],
            "clearance_limited": limited,
            "selection": "element_wise_minimum_of_incident_stable_surface_schedules",
        }
        if root_coordinates is not None:
            if root not in root_coordinates:
                raise BoundaryLayerSmokeError("scheduled prism root coordinate is missing")
            record["stable_root_coordinate_sha256"] = _stable_root_id(
                root_coordinates[root]
            )
        root_records.append(record)
    totals = [values[-1] for values in root_schedules.values()]
    return root_schedules, {
        "status": "PASS",
        "selection_basis": "stable wall fingerprint; runtime tags are audit only",
        "runtime_mesh_tags_hardcoded": False,
        "root_count": len(root_schedules),
        "shared_root_count": shared_root_count,
        "clearance_limited_root_count": limited_root_count,
        "minimum_root_total_thickness_m": min(totals),
        "maximum_root_total_thickness_m": max(totals),
        "per_surface_incident_root_counts": dict(sorted(per_surface_root_counts.items())),
        "root_assignments_audit": root_records,
    }


def _all_volume_records_for_entities(
    gmsh: Any, volume_tags: Sequence[int]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entity in volume_tags:
        for record in _element_records_from_gmsh(gmsh, 3, int(entity)):
            tag = int(record["tag"])
            if tag in seen:
                raise BoundaryLayerSmokeError("3D element tag is repeated across volumes")
            if record["type"] not in _ALLOWED_3D_TYPES:
                raise BoundaryLayerSmokeError("post-mesh repair found a disallowed 3D type")
            seen.add(tag)
            result.append(record)
    return result


def _gate_subdivision_element_cap(
    *,
    source_prism_count: int,
    nonprism_type_counts: Mapping[str, int],
    layer_count: int,
    max_3d_elements: int,
) -> int:
    prisms = _positive_int(source_prism_count, "source prism count")
    layers = _positive_int(layer_count, "subdivision layer count")
    cap = _positive_int(max_3d_elements, "subdivision element cap")
    nonprism_count = 0
    for name, raw_count in nonprism_type_counts.items():
        if str(name) not in _ALLOWED_3D_TYPES or str(name) == "Prism 6":
            raise BoundaryLayerSmokeError("subdivision nonprism inventory is invalid")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise BoundaryLayerSmokeError("subdivision nonprism count is invalid")
        nonprism_count += raw_count
    projected = nonprism_count + prisms * layers
    if projected > cap:
        raise BoundaryLayerSmokeError(
            "boundary-layer subdivision would exceed the configured element cap"
        )
    return projected


def _stable_triangle_id(
    roots: Iterable[int], coordinates: Mapping[int, Sequence[float]]
) -> str:
    points = sorted(
        tuple(round(float(value), 14) for value in coordinates[int(root)])
        for root in roots
    )
    payload = json.dumps(points, separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _stable_root_id(coordinate: Sequence[float]) -> str:
    payload = json.dumps(
        [round(float(value), 14) for value in coordinate],
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _group_repair_source_triangles(
    triangles: Iterable[Sequence[int]],
    violating_roots: Iterable[int],
    *,
    preferred_roots: Iterable[int] = (),
    triangle_group_labels: Mapping[tuple[int, int, int], str] | None = None,
    triangle_stable_ids: Mapping[tuple[int, int, int], str] | None = None,
    root_stable_ids: Mapping[int, str] | None = None,
    approved_ambiguous_owner_stable_ids: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[int, set[int]], dict[tuple[int, int, int], dict[str, Any]]]:
    """Assign quality-selected triangles to one sharp root or its one-ring.

    Direct seeds contain exactly one dynamically detected one-layer violating
    root.  A candidate without such a root can join a group only if it shares
    a complete edge with direct seed triangles belonging to exactly one root.
    The one-ring restriction prevents smoothing from walking arbitrarily
    across the wall mesh; ambiguity and disconnected candidates fail closed.
    """

    normalized = {
        tuple(sorted(int(value) for value in triangle)) for triangle in triangles
    }
    if not normalized or any(len(set(triangle)) != 3 for triangle in normalized):
        raise BoundaryLayerSmokeError("repair source triangles are invalid")
    sharp = {int(value) for value in violating_roots}
    preferred = {int(value) for value in preferred_roots}
    if not preferred <= sharp:
        raise BoundaryLayerSmokeError("preferred repair roots are not dynamic sharp roots")
    labels = {
        tuple(sorted(int(value) for value in triangle)): str(label)
        for triangle, label in (triangle_group_labels or {}).items()
    }
    if labels and set(labels) != normalized:
        raise BoundaryLayerSmokeError(
            "repair source-triangle stable group labels are incomplete"
        )
    stable_triangles = {
        tuple(sorted(int(value) for value in triangle)): str(identifier).casefold()
        for triangle, identifier in (triangle_stable_ids or {}).items()
    }
    stable_roots = {
        int(root): str(identifier).casefold()
        for root, identifier in (root_stable_ids or {}).items()
    }
    approved_owners: dict[tuple[str, str], str] = {}
    for key, root_id in (approved_ambiguous_owner_stable_ids or {}).items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise BoundaryLayerSmokeError(
                "stable ambiguous-owner approval key is invalid"
            )
        normalized_key = (str(key[0]).casefold(), str(key[1]).casefold())
        if normalized_key in approved_owners:
            raise BoundaryLayerSmokeError(
                "stable ambiguous-owner approval key is duplicated"
            )
        approved_owners[normalized_key] = str(root_id).casefold()
    if approved_owners and (
        set(stable_triangles) != normalized
        or set(labels) != normalized
        or not sharp <= set(stable_roots)
        or len(set(stable_triangles.values())) != len(stable_triangles)
        or any(_SHA256.fullmatch(value) is None for value in labels.values())
        or any(_SHA256.fullmatch(value) is None for value in stable_triangles.values())
        or any(_SHA256.fullmatch(value) is None for value in stable_roots.values())
        or any(
            _SHA256.fullmatch(value) is None
            for key in approved_owners
            for value in key
        )
        or any(_SHA256.fullmatch(value) is None for value in approved_owners.values())
    ):
        raise BoundaryLayerSmokeError(
            "stable ambiguous-owner identifiers are incomplete or invalid"
        )
    direct: dict[tuple[int, int, int], tuple[int, str]] = {}
    unresolved: list[tuple[int, int, int]] = []
    used_approved_owner_ids: Counter[tuple[str, str]] = Counter()
    for triangle in sorted(normalized):
        owners = set(triangle) & sharp
        if len(owners) > 1:
            preferred_owners = owners & preferred
            if len(preferred_owners) == 1:
                direct[triangle] = (
                    next(iter(preferred_owners)),
                    "contains_unique_strict_failure_preferred_root",
                )
                continue
            triangle_id = stable_triangles.get(triangle)
            stable_group_label = labels.get(triangle)
            approval_key = (str(triangle_id), str(stable_group_label))
            approved_root_id = approved_owners.get(approval_key)
            approved_matches = {
                root for root in owners if stable_roots.get(root) == approved_root_id
            }
            if len(preferred_owners) != 0 or len(approved_matches) != 1:
                raise BoundaryLayerSmokeError(
                    "quality-selected source triangle contains multiple sharp roots "
                    "without one unique strict-failure owner: "
                    f"triangle_node_tags_audit={list(triangle)!r}, "
                    f"sharp_root_tags_audit={sorted(owners)!r}, "
                    f"preferred_root_tags_audit={sorted(preferred_owners)!r}, "
                    f"stable_triangle_sha256={triangle_id!r}, "
                    f"stable_group_label={stable_group_label!r}, "
                    f"approved_owner_root_sha256={approved_root_id!r}"
                )
            direct[triangle] = (
                next(iter(approved_matches)),
                "matches_hash_bound_prior_strict_failure_owner",
            )
            used_approved_owner_ids[approval_key] += 1
            continue
        if owners:
            direct[triangle] = (
                next(iter(owners)),
                "contains_unique_sharp_root",
            )
        else:
            unresolved.append(triangle)
    if not direct:
        raise BoundaryLayerSmokeError(
            "quality-selected source triangles contain no dynamic sharp root"
        )
    if used_approved_owner_ids != Counter({key: 1 for key in approved_owners}):
        raise BoundaryLayerSmokeError(
            "configured ambiguous-owner evidence was not used exactly once"
        )

    assignment: dict[tuple[int, int, int], tuple[int, str]] = {
        triangle: value for triangle, value in direct.items()
    }
    for triangle in unresolved:
        neighbouring_owners = {
            value[0]
            for seed, value in direct.items()
            if len(set(triangle) & set(seed)) == 2
            and (not labels or labels[triangle] == labels[seed])
        }
        if len(neighbouring_owners) != 1:
            raise BoundaryLayerSmokeError(
                "quality-selected source triangle has no unique one-ring sharp-root "
                f"group: triangle_node_tags_audit={list(triangle)!r}, "
                f"candidate_sharp_root_tags_audit={sorted(neighbouring_owners)!r}"
            )
        assignment[triangle] = (
            next(iter(neighbouring_owners)),
            "shares_edge_with_direct_sharp_root_triangle",
        )

    groups: dict[int, set[int]] = defaultdict(set)
    evidence: dict[tuple[int, int, int], dict[str, Any]] = {}
    for triangle in sorted(assignment):
        owner, method = assignment[triangle]
        protected_sharp_roots = sorted((set(triangle) & sharp) - {owner})
        groups[owner].update(set(triangle) - {owner} - sharp)
        evidence[triangle] = {
            "sharp_root_tag_audit": owner,
            "assignment_method": method,
            "stable_group_label": labels.get(triangle),
            "protected_other_sharp_root_tags_audit": protected_sharp_roots,
        }
    return dict(groups), evidence


def _discover_repair_source_triangle_groups(
    triangles: Iterable[Sequence[int]],
    violating_roots: Iterable[int],
    *,
    preferred_roots: Iterable[int] = (),
    forced_unresolved_triangles: Iterable[Sequence[int]] = (),
    triangle_group_labels: Mapping[tuple[int, int, int], str],
    triangle_stable_ids: Mapping[tuple[int, int, int], str],
    root_stable_ids: Mapping[int, str],
    approved_ambiguous_owner_stable_ids: Mapping[tuple[str, str], str],
) -> tuple[
    dict[int, set[int]],
    dict[tuple[int, int, int], dict[str, Any]],
    list[dict[str, Any]],
]:
    """Discover repair groups without guessing an ambiguous owner.

    This helper is intentionally separate from the strict production grouping
    routine above.  It consumes the same dynamic topology but treats stale or
    missing prior-owner evidence as an observed ambiguity.  Runtime tags are
    retained only in the returned audit records; every durable identity is a
    coordinate/surface fingerprint SHA-256.
    """

    normalized = {
        tuple(sorted(int(value) for value in triangle)) for triangle in triangles
    }
    forced_unresolved = {
        tuple(sorted(int(value) for value in triangle))
        for triangle in forced_unresolved_triangles
    }
    sharp = {int(value) for value in violating_roots}
    preferred = {int(value) for value in preferred_roots}
    labels = {
        tuple(sorted(int(value) for value in triangle)): str(label).casefold()
        for triangle, label in triangle_group_labels.items()
    }
    stable_triangles = {
        tuple(sorted(int(value) for value in triangle)): str(identifier).casefold()
        for triangle, identifier in triangle_stable_ids.items()
    }
    stable_roots = {
        int(root): str(identifier).casefold()
        for root, identifier in root_stable_ids.items()
    }
    approvals = {
        (str(key[0]).casefold(), str(key[1]).casefold()): str(value).casefold()
        for key, value in approved_ambiguous_owner_stable_ids.items()
    }
    if (
        not normalized
        or any(len(set(triangle)) != 3 for triangle in normalized)
        or any(len(set(triangle)) != 3 for triangle in forced_unresolved)
        or not forced_unresolved <= normalized
        or not preferred <= sharp
        or set(labels) != normalized
        or set(stable_triangles) != normalized
        or not sharp <= set(stable_roots)
        or any(_SHA256.fullmatch(value) is None for value in labels.values())
        or any(_SHA256.fullmatch(value) is None for value in stable_triangles.values())
        or any(_SHA256.fullmatch(value) is None for value in stable_roots.values())
    ):
        raise BoundaryLayerSmokeError(
            "audit-only repair grouping identities are incomplete"
        )

    assignments: dict[tuple[int, int, int], tuple[int, str]] = {}
    unresolved: list[tuple[int, int, int]] = []
    unresolved_records: dict[tuple[int, int, int], dict[str, Any]] = {}
    for triangle in sorted(normalized):
        owners = set(triangle) & sharp
        preferred_owners = owners & preferred
        if triangle in forced_unresolved:
            if len(owners) == 1:
                raise BoundaryLayerSmokeError(
                    "forced audit ambiguity unexpectedly has one direct sharp root"
                )
            approval_key = (stable_triangles[triangle], labels[triangle])
            unresolved_records[triangle] = {
                "reason": "STRICT_FAILURE_WITHOUT_UNIQUE_DIRECT_SHARP_ROOT",
                "candidate_root_tags_audit": sorted(owners),
                "candidate_root_coordinate_sha256": sorted(
                    stable_roots[root] for root in owners
                ),
                "configured_owner_root_coordinate_sha256": approvals.get(
                    approval_key
                ),
                "one_ring_owner_inference_suppressed": True,
            }
            continue
        if len(owners) == 1:
            assignments[triangle] = (
                next(iter(owners)),
                "contains_unique_sharp_root",
            )
            continue
        if len(preferred_owners) == 1:
            assignments[triangle] = (
                next(iter(preferred_owners)),
                "contains_unique_strict_failure_preferred_root",
            )
            continue
        if len(owners) > 1:
            approval_key = (stable_triangles[triangle], labels[triangle])
            approved_root_id = approvals.get(approval_key)
            approved_matches = {
                root for root in owners if stable_roots.get(root) == approved_root_id
            }
            if len(approved_matches) == 1:
                assignments[triangle] = (
                    next(iter(approved_matches)),
                    "matches_hash_bound_prior_strict_failure_owner",
                )
                continue
            unresolved_records[triangle] = {
                "reason": "MULTIPLE_SHARP_ROOTS_WITHOUT_UNIQUE_STABLE_OWNER",
                "candidate_root_tags_audit": sorted(owners),
                "candidate_root_coordinate_sha256": sorted(
                    stable_roots[root] for root in owners
                ),
                "configured_owner_root_coordinate_sha256": approved_root_id,
            }
            continue
        unresolved.append(triangle)

    for triangle in unresolved:
        neighbouring_owners = {
            owner
            for seed, (owner, _) in assignments.items()
            if len(set(triangle) & set(seed)) == 2
            and labels[triangle] == labels[seed]
        }
        if len(neighbouring_owners) == 1:
            assignments[triangle] = (
                next(iter(neighbouring_owners)),
                "shares_edge_with_direct_sharp_root_triangle",
            )
        else:
            unresolved_records[triangle] = {
                "reason": "NO_UNIQUE_ONE_RING_SHARP_ROOT_GROUP",
                "candidate_root_tags_audit": sorted(neighbouring_owners),
                "candidate_root_coordinate_sha256": sorted(
                    stable_roots[root] for root in neighbouring_owners
                ),
                "configured_owner_root_coordinate_sha256": None,
            }

    groups: dict[int, set[int]] = defaultdict(set)
    evidence: dict[tuple[int, int, int], dict[str, Any]] = {}
    for triangle in sorted(normalized):
        if triangle in assignments:
            owner, method = assignments[triangle]
            protected = sorted((set(triangle) & sharp) - {owner})
            groups[owner].update(set(triangle) - {owner} - sharp)
            evidence[triangle] = {
                "sharp_root_tag_audit": owner,
                "assignment_method": method,
                "stable_group_label": labels[triangle],
                "protected_other_sharp_root_tags_audit": protected,
            }
        else:
            record = unresolved_records[triangle]
            evidence[triangle] = {
                "sharp_root_tag_audit": None,
                "assignment_method": "AUDIT_ONLY_UNRESOLVED",
                "stable_group_label": labels[triangle],
                "protected_other_sharp_root_tags_audit": record[
                    "candidate_root_tags_audit"
                ],
            }

    ambiguities = [
        {
            "source_triangle_sha256": stable_triangles[triangle],
            "wall_surface_fingerprint": labels[triangle],
            **record,
        }
        for triangle, record in sorted(
            unresolved_records.items(), key=lambda item: stable_triangles[item[0]]
        )
    ]
    return dict(groups), evidence, ambiguities


def _mean_unit_direction(
    directions: Sequence[Sequence[float]], *, label: str
) -> tuple[float, float, float]:
    if not directions:
        raise BoundaryLayerSmokeError(f"{label} has no direction vectors")
    accumulated = (0.0, 0.0, 0.0)
    for raw in directions:
        unit = _unit_vector(raw)
        if unit is None:
            raise BoundaryLayerSmokeError(f"{label} contains a zero direction")
        accumulated = _vector_add(accumulated, unit)
    result = _unit_vector(accumulated)
    if result is None:
        raise BoundaryLayerSmokeError(f"{label} has a zero vector sum")
    return result


def _owner_free_quality_snapshot(
    gmsh: Any,
    *,
    prism_element_tags: Sequence[int],
    core_element_tags: Sequence[int],
    core_tetra_element_tags: Sequence[int],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
    include_deficit_metrics: bool = False,
    prism_scaled_jacobian_by_tag: dict[int, float] | None = None,
) -> dict[str, Any]:
    """Query one deterministic quality state without hiding non-finite values."""

    prism_tags = sorted({int(value) for value in prism_element_tags})
    core_tags = sorted({int(value) for value in core_element_tags})
    tetra_tags = sorted({int(value) for value in core_tetra_element_tags})
    prism_threshold = _finite(
        minimum_prism_scaled_jacobian,
        "owner-free minimum prism scaled Jacobian",
    )
    core_threshold = _finite(
        minimum_core_tetra_gamma,
        "owner-free minimum core tetra gamma",
    )
    if (
        not prism_tags
        or not core_tags
        or not tetra_tags
        or set(prism_tags) & set(core_tags)
        or not set(tetra_tags) <= set(core_tags)
        or prism_threshold <= 0.0
        or core_threshold <= 0.0
    ):
        raise BoundaryLayerSmokeError(
            "owner-free quality query has incomplete element coverage"
        )

    all_tags = [*prism_tags, *core_tags]
    prism_tag_set = set(prism_tags)
    nonfinite_tags: set[int] = set()
    nonpositive_tags: set[int] = set()
    min_sj_by_tag: dict[int, float] = {}
    for metric in ("minDetJac", "minSJ", "minSICN", "volume"):
        values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(all_tags, metric)
        ]
        if len(values) != len(all_tags):
            raise BoundaryLayerSmokeError(
                f"owner-free Gmsh quality {metric} count is inconsistent"
            )
        for tag, value in zip(all_tags, values):
            if not math.isfinite(value):
                nonfinite_tags.add(tag)
            elif value <= 0.0:
                nonpositive_tags.add(tag)
            if metric == "minSJ" and tag in prism_tag_set:
                min_sj_by_tag[tag] = value
    gamma_values = [
        float(value)
        for value in gmsh.model.mesh.getElementQualities(tetra_tags, "gamma")
    ]
    if len(gamma_values) != len(tetra_tags):
        raise BoundaryLayerSmokeError(
            "owner-free Gmsh core gamma count is inconsistent"
        )
    gamma_by_tag = dict(zip(tetra_tags, gamma_values))
    for tag, value in gamma_by_tag.items():
        if not math.isfinite(value):
            nonfinite_tags.add(tag)
        elif value <= 0.0:
            nonpositive_tags.add(tag)

    if prism_scaled_jacobian_by_tag is not None:
        if prism_scaled_jacobian_by_tag:
            raise BoundaryLayerSmokeError(
                "owner-free Prism6 quality collector is not empty"
            )
        prism_scaled_jacobian_by_tag.update(min_sj_by_tag)

    finite_sj = [value for value in min_sj_by_tag.values() if math.isfinite(value)]
    finite_gamma = [value for value in gamma_by_tag.values() if math.isfinite(value)]
    minimum_sj = min(finite_sj) if finite_sj else -1.0e300
    minimum_gamma = min(finite_gamma) if finite_gamma else -1.0e300
    below_prism = sum(
        1
        for value in min_sj_by_tag.values()
        if not math.isfinite(value) or value < prism_threshold
    )
    below_core = sum(
        1
        for value in gamma_by_tag.values()
        if not math.isfinite(value) or value < core_threshold
    )
    unsigned: dict[str, Any] = {
        "status": (
            "PASS"
            if not nonfinite_tags
            and not nonpositive_tags
            and below_prism == 0
            and below_core == 0
            else "FAIL"
        ),
        "minimum_prism_scaled_jacobian_for_pass": prism_threshold,
        "minimum_core_tetra_gamma_for_pass": core_threshold,
        "all_values_finite": not nonfinite_tags,
        "nonfinite_count": len(nonfinite_tags),
        "nonpositive_element_count": len(nonpositive_tags),
        "prism_below_threshold_element_count": below_prism,
        "core_tetra_below_gamma_count": below_core,
        "minimum_prism_scaled_jacobian": minimum_sj,
        "minimum_core_tetra_gamma": minimum_gamma,
        "prism_element_count": len(prism_tags),
        "core_element_count": len(core_tags),
        "core_tetra_count": len(tetra_tags),
    }
    if include_deficit_metrics:
        prism_deficits = sorted(
            max(0.0, prism_threshold - value)
            for value in finite_sj
        )
        core_deficits = sorted(
            max(0.0, core_threshold - value)
            for value in finite_gamma
        )
        unsigned.update(
            {
                "maximum_prism_scaled_jacobian_deficit": (
                    max(prism_deficits, default=0.0)
                ),
                "prism_scaled_jacobian_deficit_l1": math.fsum(
                    prism_deficits
                ),
                "prism_scaled_jacobian_deficit_l2": math.fsum(
                    value * value for value in prism_deficits
                ),
                "maximum_core_tetra_gamma_deficit": max(
                    core_deficits, default=0.0
                ),
                "core_tetra_gamma_deficit_l2": math.fsum(
                    value * value for value in core_deficits
                ),
            }
        )
    return {**unsigned, "quality_sha256": _canonical_hash(unsigned)}


_OWNER_FREE_BASE_QUALITY_OBJECTIVE = (
    "nonfinite_count",
    "nonpositive_element_count",
    "prism_below_minimum_scaled_jacobian_count",
    "core_tetra_below_minimum_gamma_count",
    "negative_minimum_prism_scaled_jacobian",
    "negative_minimum_core_tetra_gamma",
    "direction_change_penalty",
)
_OWNER_FREE_CONTINUOUS_QUALITY_OBJECTIVE = (
    "nonfinite_count",
    "nonpositive_element_count",
    "core_tetra_below_minimum_gamma_count",
    "maximum_core_tetra_gamma_deficit",
    "core_tetra_gamma_deficit_l2",
    "maximum_prism_scaled_jacobian_deficit",
    "prism_scaled_jacobian_deficit_l2",
    "prism_scaled_jacobian_deficit_l1",
    "prism_below_minimum_scaled_jacobian_count",
    "negative_minimum_prism_scaled_jacobian",
    "negative_minimum_core_tetra_gamma",
    "direction_change_penalty",
)
_OWNER_FREE_QUALITY_VALUE_FIELDS = {
    "nonfinite_count": ("nonfinite_count", 1.0),
    "nonpositive_element_count": ("nonpositive_element_count", 1.0),
    "prism_below_minimum_scaled_jacobian_count": (
        "prism_below_threshold_element_count",
        1.0,
    ),
    "core_tetra_below_minimum_gamma_count": (
        "core_tetra_below_gamma_count",
        1.0,
    ),
    "maximum_core_tetra_gamma_deficit": (
        "maximum_core_tetra_gamma_deficit",
        1.0,
    ),
    "core_tetra_gamma_deficit_l2": ("core_tetra_gamma_deficit_l2", 1.0),
    "maximum_prism_scaled_jacobian_deficit": (
        "maximum_prism_scaled_jacobian_deficit",
        1.0,
    ),
    "prism_scaled_jacobian_deficit_l2": (
        "prism_scaled_jacobian_deficit_l2",
        1.0,
    ),
    "prism_scaled_jacobian_deficit_l1": (
        "prism_scaled_jacobian_deficit_l1",
        1.0,
    ),
    "negative_minimum_prism_scaled_jacobian": (
        "minimum_prism_scaled_jacobian",
        -1.0,
    ),
    "negative_minimum_core_tetra_gamma": (
        "minimum_core_tetra_gamma",
        -1.0,
    ),
}
_OWNER_FREE_QUALITY_COUNT_FIELDS = (
    "nonfinite_count",
    "nonpositive_element_count",
    "core_tetra_below_minimum_gamma_count",
    "prism_below_minimum_scaled_jacobian_count",
)


def _validated_owner_free_quality_objective_fields(
    quality_objective: Sequence[str] | None,
    *,
    continuous_deficit_merit: bool,
) -> tuple[str, ...]:
    if quality_objective is None:
        return (
            _OWNER_FREE_CONTINUOUS_QUALITY_OBJECTIVE
            if continuous_deficit_merit
            else _OWNER_FREE_BASE_QUALITY_OBJECTIVE
        )
    if isinstance(quality_objective, (str, bytes)) or not isinstance(
        quality_objective, Sequence
    ):
        raise BoundaryLayerSmokeError(
            "owner-free quality objective declaration is invalid"
        )
    fields = tuple(quality_objective)
    if any(not isinstance(field, str) for field in fields):
        raise BoundaryLayerSmokeError(
            "owner-free quality objective declaration is invalid"
        )
    if len(fields) != len(set(fields)):
        raise BoundaryLayerSmokeError(
            "owner-free quality objective declaration contains duplicates"
        )
    allowed_fields = {
        *_OWNER_FREE_QUALITY_VALUE_FIELDS,
        "direction_change_penalty",
    }
    unknown_fields = sorted(set(fields) - allowed_fields)
    if unknown_fields:
        raise BoundaryLayerSmokeError(
            "owner-free quality objective declaration contains unknown fields: "
            + ", ".join(unknown_fields)
        )
    field_set = frozenset(fields)
    complete_field_sets = {
        frozenset(_OWNER_FREE_BASE_QUALITY_OBJECTIVE),
        frozenset(_OWNER_FREE_CONTINUOUS_QUALITY_OBJECTIVE),
    }
    if field_set not in complete_field_sets:
        raise BoundaryLayerSmokeError(
            "owner-free quality objective declaration is incomplete"
        )
    return fields


def _owner_free_quality_objective(
    quality: Mapping[str, Any],
    *,
    direction_change_penalty: float,
    continuous_deficit_merit: bool = False,
    quality_objective: Sequence[str] | None = None,
) -> tuple[float, ...]:
    penalty = _finite(direction_change_penalty, "owner-free direction penalty")
    if penalty < 0.0:
        raise BoundaryLayerSmokeError("owner-free direction penalty is negative")
    fields = _validated_owner_free_quality_objective_fields(
        quality_objective,
        continuous_deficit_merit=continuous_deficit_merit,
    )
    values: list[float] = []
    for field in fields:
        if field == "direction_change_penalty":
            values.append(penalty)
            continue
        quality_field, multiplier = _OWNER_FREE_QUALITY_VALUE_FIELDS[field]
        try:
            values.append(multiplier * float(quality[quality_field]))
        except (KeyError, TypeError, ValueError) as error:
            raise BoundaryLayerSmokeError(
                "owner-free quality objective input is incomplete or invalid"
            ) from error
    return tuple(values)


def _run_owner_free_component_direction_search(
    gmsh: Any,
    *,
    components: Sequence[Sequence[int]],
    stable_components: Sequence[Mapping[str, Any]],
    bad_source_triangles: Sequence[tuple[int, int, int]],
    triangle_stable_ids: Mapping[tuple[int, int, int], str],
    triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    prism_volume_fingerprints: Mapping[int, str],
    source_triangle_normals: Mapping[tuple[int, int, int], Sequence[float]],
    incident_normals: Mapping[int, Sequence[Sequence[float]]],
    original_directions: Mapping[int, Sequence[float]],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    root_cumulative_heights: Mapping[int, Sequence[float]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
    minimum_cone_margin: float,
    minimum_changed_direction_margin: float,
    endpoint: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[int, tuple[float, float, float]]]:
    """Run the bounded owner-free search with absolute A-B-A coordinate replay."""

    base_runtime_components = [
        tuple(int(root) for root in item) for item in components
    ]
    if len(base_runtime_components) != len(stable_components):
        raise BoundaryLayerSmokeError("owner-free component evidence is misaligned")
    runtime_components = list(base_runtime_components)
    search_stable_components = [dict(item) for item in stable_components]
    candidate_roots = sorted(
        {root for item in base_runtime_components for root in item}
    )
    endpoint_schema = endpoint.get("schema")
    triple_refinement_endpoint = (
        endpoint_schema == TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA
    )
    fine_refinement_endpoint = endpoint_schema in {
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    }
    pattern_endpoint = endpoint_schema in {
        PATTERN_ENDPOINT_SCHEMA,
        FRONTIER_PATTERN_ENDPOINT_SCHEMA,
        FINE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
        TRIPLE_REFINEMENT_PATTERN_ENDPOINT_SCHEMA,
    }
    try:
        raw_quality_objective = endpoint["quality_objective"]
    except KeyError as error:
        raise BoundaryLayerSmokeError(
            "owner-free endpoint quality objective is missing"
        ) from error
    objective_fields = _validated_owner_free_quality_objective_fields(
        raw_quality_objective,
        continuous_deficit_merit=pattern_endpoint,
    )
    required_cone_margin = _finite(
        minimum_cone_margin, "owner-free minimum cone margin"
    )
    required_changed_direction_margin = _finite(
        minimum_changed_direction_margin,
        "owner-free minimum changed-direction margin",
    )
    if (
        not candidate_roots
        or len(base_runtime_components)
        > int(endpoint["maximum_component_count"])
        or len(candidate_roots) > int(endpoint["maximum_candidate_root_count"])
        or any(root not in chains for root in candidate_roots)
        or any(root not in original_directions for root in candidate_roots)
        or any(root not in incident_normals for root in candidate_roots)
        or required_cone_margin <= 0.0
        or required_changed_direction_margin < required_cone_margin
    ):
        raise BoundaryLayerSmokeError("owner-free component search exceeds its endpoint")

    original = {
        root: _unit_vector(original_directions[root]) for root in candidate_roots
    }
    if any(direction is None for direction in original.values()):
        raise BoundaryLayerSmokeError("owner-free original direction is zero")
    original = {root: direction for root, direction in original.items() if direction is not None}

    def direction_margin(root: int, direction: Sequence[float]) -> float:
        unit = _unit_vector(direction)
        if unit is None:
            raise BoundaryLayerSmokeError(
                "owner-free direction margin received a zero vector"
            )
        return min(
            _vector_dot(_unit_vector(normal) or (0.0, 0.0, 0.0), unit)
            for normal in incident_normals[root]
        )

    original_minimum_direction_margin = min(
        direction_margin(root, original[root]) for root in candidate_roots
    )
    if (
        pattern_endpoint
        and original_minimum_direction_margin < required_cone_margin
    ):
        raise BoundaryLayerSmokeError(
            "owner-free entry direction violates the configured cone margin"
        )

    chain_node_to_roots: dict[int, set[int]] = defaultdict(set)
    for root in candidate_roots:
        for node in chains[root]:
            chain_node_to_roots[int(node)].add(root)
    prism_tags_by_root: dict[int, set[int]] = defaultdict(set)
    for record in subdivided_prisms:
        touched = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        for root in touched:
            prism_tags_by_root[root].add(int(record["tag"]))
    core_tags_by_root: dict[int, set[int]] = defaultdict(set)
    core_tetra_tags_by_root: dict[int, set[int]] = defaultdict(set)
    for record in core_volume_records:
        touched = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        for root in touched:
            core_tags_by_root[root].add(int(record["tag"]))
            if record["type"] == "Tetrahedron 4":
                core_tetra_tags_by_root[root].add(int(record["tag"]))
    if any(
        not prism_tags_by_root[root]
        or not core_tags_by_root[root]
        or not core_tetra_tags_by_root[root]
        for root in candidate_roots
    ):
        raise BoundaryLayerSmokeError(
            "owner-free candidate root has incomplete prism/core adjacency"
        )

    all_chain_origin: dict[int, tuple[int, int]] = {}
    for root, chain in chains.items():
        for layer, node in enumerate(chain):
            previous = all_chain_origin.setdefault(int(node), (int(root), layer))
            if previous != (int(root), layer):
                raise BoundaryLayerSmokeError(
                    "owner-free layer chains share a runtime node"
                )

    def prism_lineage(record: Mapping[str, Any]) -> dict[str, Any]:
        mapped = [all_chain_origin.get(int(node)) for node in record["nodes"]]
        if any(value is None for value in mapped):
            raise BoundaryLayerSmokeError(
                "owner-free affected Prism6 is not chain-mappable"
            )
        roots = sorted({int(value[0]) for value in mapped if value is not None})
        levels = Counter(int(value[1]) for value in mapped if value is not None)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError(
                "owner-free affected Prism6 has invalid layer lineage"
            )
        entity = int(record["entity"])
        try:
            wall_fingerprint = str(prism_volume_fingerprints[entity])
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "owner-free affected Prism6 lacks a stable wall fingerprint"
            ) from error
        stable = {
            "kind": "Prism6",
            "wall_surface_fingerprint": wall_fingerprint,
            "source_triangle_sha256": _stable_triangle_id(
                roots, root_coordinates
            ),
            "root_coordinate_sha256": sorted(
                _stable_root_id(root_coordinates[root]) for root in roots
            ),
            "layer_1_based": min(levels) + 1,
        }
        return {
            **stable,
            "prism_lineage_sha256": _canonical_hash(stable),
        }

    def core_lineage_sha256(record: Mapping[str, Any]) -> str:
        tokens: list[list[Any]] = []
        for raw_node in record["nodes"]:
            node = int(raw_node)
            mapped = all_chain_origin.get(node)
            if mapped is not None:
                root, layer = mapped
                tokens.append(
                    [
                        "chain",
                        _stable_root_id(root_coordinates[root]),
                        int(layer),
                    ]
                )
            else:
                coordinates_value = gmsh.model.mesh.getNode(node)[0]
                tokens.append(
                    [
                        "fixed",
                        _stable_root_id(coordinates_value),
                    ]
                )
        stable = {
            "kind": str(record["type"]),
            "node_lineage": sorted(tokens, key=lambda item: tuple(item)),
        }
        return _canonical_hash(stable)

    interaction_topology: dict[str, Any] | None = None
    component_element_sets: dict[str, dict[str, set[int]]] = {}
    interaction_component_by_root: dict[int, str] = {}
    if pattern_endpoint:
        parent = {root: root for root in candidate_roots}

        def find(root: int) -> int:
            while parent[root] != root:
                parent[root] = parent[parent[root]]
                root = parent[root]
            return root

        def union(roots: Iterable[int]) -> None:
            values = sorted({int(root) for root in roots})
            if not values:
                return
            anchor = find(values[0])
            for root in values[1:]:
                other = find(root)
                if anchor != other:
                    parent[max(anchor, other)] = min(anchor, other)
                    anchor = find(anchor)

        base_component_by_root: dict[int, str] = {}
        for component, stable in zip(base_runtime_components, stable_components):
            component_sha = str(stable["component_sha256"])
            union(component)
            for root in component:
                if root in base_component_by_root:
                    raise BoundaryLayerSmokeError(
                        "owner-free base components overlap"
                    )
                base_component_by_root[root] = component_sha

        affected_prism_roots: dict[int, set[int]] = {}
        affected_core_roots: dict[int, set[int]] = {}
        prism_records_by_tag = {
            int(record["tag"]): record for record in subdivided_prisms
        }
        core_records_by_tag = {
            int(record["tag"]): record for record in core_volume_records
        }
        for tag, record in prism_records_by_tag.items():
            roots = {
                root
                for node in record["nodes"]
                for root in chain_node_to_roots.get(int(node), set())
            }
            if roots:
                affected_prism_roots[tag] = roots
                union(roots)
        for tag, record in core_records_by_tag.items():
            roots = {
                root
                for node in record["nodes"]
                for root in chain_node_to_roots.get(int(node), set())
            }
            if roots:
                affected_core_roots[tag] = roots
                union(roots)

        grouped: dict[int, list[int]] = defaultdict(list)
        for root in candidate_roots:
            grouped[find(root)].append(root)
        interaction_components = sorted(
            (tuple(sorted(values)) for values in grouped.values()),
            key=lambda roots: tuple(
                sorted(_stable_root_id(root_coordinates[root]) for root in roots)
            ),
        )
        interaction_stable_components: list[dict[str, Any]] = []
        cross_base_prism = 0
        cross_base_core = 0
        all_element_lineages: set[str] = set()
        for roots in interaction_components:
            root_set = set(roots)
            source_component_ids = sorted(
                {base_component_by_root[root] for root in roots}
            )
            affected_prism_tags = {
                tag
                for tag, touched in affected_prism_roots.items()
                if touched & root_set
            }
            affected_core_tags = {
                tag
                for tag, touched in affected_core_roots.items()
                if touched & root_set
            }
            if any(
                not touched <= root_set
                for tag, touched in affected_prism_roots.items()
                if tag in affected_prism_tags
            ) or any(
                not touched <= root_set
                for tag, touched in affected_core_roots.items()
                if tag in affected_core_tags
            ):
                raise BoundaryLayerSmokeError(
                    "owner-free affected element crosses interaction components"
                )
            prism_lineages = [
                prism_lineage(prism_records_by_tag[tag])[
                    "prism_lineage_sha256"
                ]
                for tag in sorted(affected_prism_tags)
            ]
            core_lineages = [
                core_lineage_sha256(core_records_by_tag[tag])
                for tag in sorted(affected_core_tags)
            ]
            element_lineages = sorted([*prism_lineages, *core_lineages])
            if len(element_lineages) != len(set(element_lineages)):
                raise BoundaryLayerSmokeError(
                    "owner-free affected element lineage is not unique"
                )
            all_element_lineages.update(element_lineages)
            stable_unsigned = {
                "root_coordinate_sha256": sorted(
                    _stable_root_id(root_coordinates[root]) for root in roots
                ),
                "candidate_root_count": len(roots),
                "source_component_sha256": source_component_ids,
                "source_component_count": len(source_component_ids),
                "affected_prism_count": len(affected_prism_tags),
                "affected_core_element_count": len(affected_core_tags),
                "affected_core_tetra_count": sum(
                    core_records_by_tag[tag]["type"] == "Tetrahedron 4"
                    for tag in affected_core_tags
                ),
                "affected_element_lineage_sha256": _canonical_hash(
                    {"element_sha256": element_lineages}
                ),
                "connected_by_affected_element_cooccurrence": True,
                "sharp_owner_used": False,
                "split_by_wall_fingerprint": False,
            }
            component_sha = _canonical_hash(stable_unsigned)
            stable_record = {
                **stable_unsigned,
                "interaction_component_sha256": component_sha,
            }
            interaction_stable_components.append(stable_record)
            component_element_sets[component_sha] = {
                "prism": affected_prism_tags,
                "core": affected_core_tags,
                "tetra": {
                    tag
                    for tag in affected_core_tags
                    if core_records_by_tag[tag]["type"] == "Tetrahedron 4"
                },
            }
            for root in roots:
                interaction_component_by_root[root] = component_sha

        for touched in affected_prism_roots.values():
            if len({base_component_by_root[root] for root in touched}) > 1:
                cross_base_prism += 1
        for touched in affected_core_roots.values():
            if len({base_component_by_root[root] for root in touched}) > 1:
                cross_base_core += 1
        if (
            set(interaction_component_by_root) != set(candidate_roots)
            or sum(
                len(values["prism"]) + len(values["core"])
                for values in component_element_sets.values()
            )
            != len(all_element_lineages)
        ):
            raise BoundaryLayerSmokeError(
                "owner-free interaction component coverage is incomplete"
            )
        topology_unsigned = {
            "schema": "cfdpipe.owner_free_interaction_topology.v1",
            "status": "PASS",
            "base_component_count": len(base_runtime_components),
            "interaction_component_count": len(interaction_components),
            "candidate_root_count": len(candidate_roots),
            "candidate_chain_node_count": sum(
                len(chains[root]) for root in candidate_roots
            ),
            "affected_prism_count": len(affected_prism_roots),
            "affected_core_element_count": len(affected_core_roots),
            "affected_core_tetra_count": sum(
                record["type"] == "Tetrahedron 4"
                for tag, record in core_records_by_tag.items()
                if tag in affected_core_roots
            ),
            "cross_base_component_prism_count": cross_base_prism,
            "cross_base_component_core_count": cross_base_core,
            "base_components_merged": (
                len(interaction_components) < len(base_runtime_components)
            ),
            "components": sorted(
                interaction_stable_components,
                key=lambda item: item["interaction_component_sha256"],
            ),
        }
        interaction_topology = {
            **topology_unsigned,
            "interaction_topology_sha256": _canonical_hash(topology_unsigned),
        }
        runtime_components = interaction_components
        search_stable_components = interaction_stable_components

    all_prism_tags = sorted(int(record["tag"]) for record in subdivided_prisms)
    all_core_tags = sorted(int(record["tag"]) for record in core_volume_records)
    all_core_tetra_tags = sorted(
        int(record["tag"])
        for record in core_volume_records
        if record["type"] == "Tetrahedron 4"
    )
    endpoint_sources = set(str(value) for value in endpoint["candidate_sources"])
    weights = [float(value) for value in endpoint["original_direction_interior_weights"]]
    endpoint_maximum_evaluations = int(endpoint["maximum_quality_evaluations"])
    if fine_refinement_endpoint:
        if endpoint_maximum_evaluations != 4096:
            raise BoundaryLayerSmokeError(
                "owner-free fine-refinement endpoint quality cap differs"
            )
        maximum_evaluations = min(
            endpoint_maximum_evaluations,
            (
                _TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
                if triple_refinement_endpoint
                else _FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
            ),
        )
    else:
        maximum_evaluations = endpoint_maximum_evaluations
    evaluation_count = 0
    rejected_margin_candidate_count = 0

    def set_state(state: Mapping[int, Sequence[float]]) -> None:
        for root in sorted(state):
            direction = _unit_vector(state[root])
            if direction is None:
                raise BoundaryLayerSmokeError("owner-free candidate direction is zero")
            origin = root_coordinates[root]
            schedule = root_cumulative_heights[root]
            if len(chains[root]) != len(schedule) + 1:
                raise BoundaryLayerSmokeError(
                    "owner-free root chain/schedule lengths differ"
                )
            for layer, node in enumerate(chains[root][1:]):
                target = _vector_add(
                    origin, _vector_scale(direction, float(schedule[layer]))
                )
                existing = gmsh.model.mesh.getNode(int(node))
                gmsh.model.mesh.setNode(
                    int(node), list(target), list(existing[1])
                )

    def evaluate(
        roots: Sequence[int], state: Mapping[int, Sequence[float]]
    ) -> tuple[dict[str, Any], tuple[float, ...]]:
        nonlocal evaluation_count
        if evaluation_count >= maximum_evaluations:
            raise BoundaryLayerSmokeError(
                "owner-free quality evaluation cap was exhausted"
            )
        set_state(state)
        prism_tags = sorted(
            {tag for root in roots for tag in prism_tags_by_root[int(root)]}
        )
        core_tags = sorted(
            {tag for root in roots for tag in core_tags_by_root[int(root)]}
        )
        tetra_tags = sorted(
            {tag for root in roots for tag in core_tetra_tags_by_root[int(root)]}
        )
        try:
            quality = _owner_free_quality_snapshot(
                gmsh,
                prism_element_tags=prism_tags,
                core_element_tags=core_tags,
                core_tetra_element_tags=tetra_tags,
                minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
                minimum_core_tetra_gamma=minimum_core_tetra_gamma,
                include_deficit_metrics=pattern_endpoint,
            )
            penalty = sum(
                max(
                    0.0,
                    1.0
                    - _vector_dot(
                        original[int(root)],
                        _unit_vector(state[int(root)]) or original[int(root)],
                    ),
                )
                for root in roots
            )
            objective = _owner_free_quality_objective(
                quality,
                direction_change_penalty=penalty,
                quality_objective=objective_fields,
            )
        except BaseException:
            set_state(original)
            raise
        evaluation_count += 1
        return quality, objective

    def direction_key(direction: Sequence[float]) -> tuple[float, float, float]:
        unit = _unit_vector(direction)
        if unit is None:
            raise BoundaryLayerSmokeError("owner-free candidate direction is zero")
        return tuple(round(value, 15) for value in unit)

    def coordinate_state_sha256(roots: Sequence[int]) -> str:
        records: list[dict[str, Any]] = []
        for root in sorted(
            {int(value) for value in roots},
            key=lambda value: _stable_root_id(root_coordinates[value]),
        ):
            root_sha = _stable_root_id(root_coordinates[root])
            for layer, node in enumerate(chains[root]):
                raw_coordinates = gmsh.model.mesh.getNode(int(node))[0]
                values = [float(value) for value in raw_coordinates]
                if len(values) != 3 or any(
                    not math.isfinite(value) for value in values
                ):
                    raise BoundaryLayerSmokeError(
                        "owner-free coordinate replay contains a non-finite node"
                    )
                records.append(
                    {
                        "root_coordinate_sha256": root_sha,
                        "layer": layer,
                        "coordinates_float_hex": [
                            value.hex() for value in values
                        ],
                    }
                )
        return _canonical_hash(
            {
                "encoding": "python-float-hex/root-sha/layer-v1",
                "records": records,
            }
        )

    def required_margin_for_candidate(
        root: int, direction: Sequence[float]
    ) -> float:
        return (
            required_cone_margin
            if direction_key(direction) == direction_key(original[root])
            else required_changed_direction_margin
        )

    component_records: list[dict[str, Any]] = []
    selected_state = dict(original)
    selected_metadata: dict[int, tuple[str, float, int]] = {
        root: ("original_direction", 1.0, 1) for root in candidate_roots
    }
    candidate_count_by_root: dict[int, int] = {}
    pattern_steps = [
        float(value) for value in endpoint.get("tangent_pattern_steps_radians", [])
    ]
    pattern_evaluation_count = 0
    pattern_accepted_move_count = 0
    pattern_refined_component_count = 0
    fine_component_contexts: dict[str, dict[str, Any]] = {}
    fine_component_static_evidence: list[dict[str, Any]] = []
    fine_static_component_order: list[str] = []
    fine_refinement_selection: dict[str, Any] | None = None
    triple_refinement_selection: dict[str, Any] | None = None
    triple_step_pass_records: list[dict[str, Any]] = []
    triple_quality_evaluation_count = 0
    triple_accepted_move_count = 0
    triple_rejected_margin_candidate_count = 0

    triangles_by_root: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for triangle in bad_source_triangles:
        for root in triangle:
            if root in original:
                triangles_by_root[root].append(triangle)

    if fine_refinement_endpoint:
        # Phase 1 is always rebuilt from the fresh incoming A direction field.
        # No persisted search state, tangent option, or pair option is consumed.
        set_state(original)

    for component, stable_component in zip(
        runtime_components, search_stable_components
    ):
        component_roots = tuple(sorted(component))
        component_triangles = sorted(
            {
                triangle
                for root in component_roots
                for triangle in triangles_by_root.get(root, [])
                if set(triangle) <= set(component_roots)
            }
        )
        component_normals = [
            source_triangle_normals[triangle] for triangle in component_triangles
        ]
        component_edges = sorted(
            {
                tuple(sorted((first, second)))
                for triangle in component_triangles
                for first_index, first in enumerate(triangle)
                for second in triangle[first_index + 1 :]
                if first in component_roots and second in component_roots
            }
        )
        component_targets: list[tuple[str, tuple[float, float, float]]] = []
        component_targets.append(
            (
                "component_chebyshev",
                _chebyshev_cone_direction(
                    [normal for root in component_roots for normal in incident_normals[root]]
                )[1],
            )
        )
        component_targets.append(
            (
                "component_mean_bad_triangle_normal",
                _mean_unit_direction(
                    component_normals, label="component bad-triangle normals"
                ),
            )
        )
        component_targets.append(
            (
                "component_mean_original_direction",
                _mean_unit_direction(
                    [original[root] for root in component_roots],
                    label="component original directions",
                ),
            )
        )
        if any(source not in endpoint_sources for source, _ in component_targets):
            raise BoundaryLayerSmokeError(
                "owner-free component target is outside the endpoint"
            )

        candidates_by_root: dict[
            int,
            list[tuple[tuple[float, float, float], str, float]],
        ] = {}
        for root in component_roots:
            root_targets = list(component_targets)
            root_targets.extend(
                [
                    (
                        "incident_chebyshev",
                        _chebyshev_cone_direction(incident_normals[root])[1],
                    ),
                    (
                        "incident_mean_normal",
                        _mean_unit_direction(
                            list(incident_normals[root]),
                            label="incident normals",
                        ),
                    ),
                ]
            )
            for triangle in sorted(triangles_by_root[root]):
                root_targets.append(
                    ("local_bad_triangle_normal", source_triangle_normals[triangle])
                )
            neighbours = sorted(
                {
                    neighbour
                    for triangle in triangles_by_root[root]
                    for neighbour in triangle
                    if neighbour != root and neighbour in original
                }
            )
            if neighbours:
                root_targets.append(
                    (
                        "neighbour_mean_original_direction",
                        _mean_unit_direction(
                            [original[neighbour] for neighbour in neighbours],
                            label="neighbour original directions",
                        ),
                    )
                )
            if any(source not in endpoint_sources for source, _ in root_targets):
                raise BoundaryLayerSmokeError(
                    "owner-free root target is outside the endpoint"
                )
            unique: dict[
                tuple[float, float, float],
                tuple[tuple[float, float, float], str, float],
            ] = {
                direction_key(original[root]): (
                    original[root],
                    "original_direction",
                    1.0,
                )
            }
            for source, target in root_targets:
                for weight in weights:
                    candidate, margin, _alignment, _candidate_count = (
                        _closest_feasible_cone_direction(
                        target,
                        incident_normals[root],
                        original[root],
                        interior_weight=weight,
                        )
                    )
                    if pattern_endpoint and margin < required_margin_for_candidate(
                        root, candidate
                    ):
                        rejected_margin_candidate_count += 1
                        continue
                    key = direction_key(candidate)
                    record = (candidate, source, weight)
                    previous = unique.get(key)
                    if previous is None or (source, weight) < (previous[1], previous[2]):
                        unique[key] = record
            candidates_by_root[root] = sorted(
                unique.values(),
                key=lambda item: (item[1], item[2], direction_key(item[0])),
            )
            candidate_count_by_root[root] = len(candidates_by_root[root])

        if pattern_endpoint:
            static_upper_bound = (
                1
                + len(component_targets) * len(weights)
                + int(endpoint["coordinate_descent_passes"])
                * sum(
                    max(0, len(candidates_by_root[root]) - 1)
                    for root in component_roots
                )
                + 1
            )
            replay_and_final_reserve = 6 + len(runtime_components)
            if fine_refinement_endpoint:
                budget_required = (
                    evaluation_count
                    + static_upper_bound
                    + replay_and_final_reserve
                )
            else:
                pattern_upper_bound = (
                    len(pattern_steps)
                    * int(endpoint["tangent_coordinate_passes_per_step"])
                    * (
                        4 * len(component_roots)
                        + int(endpoint["maximum_pair_candidates_per_edge_step"])
                        * len(component_edges)
                    )
                )
                budget_required = (
                    evaluation_count
                    + static_upper_bound
                    + pattern_upper_bound
                    + replay_and_final_reserve
                )
            if budget_required > maximum_evaluations:
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free pre-mutation evaluation budget is insufficient"
                )

        component_start_evaluations = evaluation_count
        current_state = {root: selected_state[root] for root in component_roots}
        best_quality, best_objective = evaluate(component_roots, current_state)
        best_quality_evaluation_index = evaluation_count
        best_rank = (
            best_objective,
            tuple(direction_key(current_state[root]) for root in component_roots),
        )
        best_metadata = {
            root: selected_metadata[root] for root in component_roots
        }
        joint_candidate_count = 0
        for source, target in component_targets:
            for weight in weights:
                candidate_state: dict[int, tuple[float, float, float]] = {}
                joint_safe = True
                for root in component_roots:
                    candidate, margin, _alignment, _candidate_count = (
                        _closest_feasible_cone_direction(
                        target,
                        incident_normals[root],
                        original[root],
                        interior_weight=weight,
                        )
                    )
                    if pattern_endpoint and margin < required_margin_for_candidate(
                        root, candidate
                    ):
                        rejected_margin_candidate_count += 1
                        joint_safe = False
                        break
                    candidate_state[root] = candidate
                if not joint_safe:
                    continue
                joint_candidate_count += 1
                candidate_quality, objective = evaluate(
                    component_roots, candidate_state
                )
                rank = (
                    objective,
                    tuple(
                        direction_key(candidate_state[root])
                        for root in component_roots
                    ),
                )
                if rank < best_rank:
                    best_rank = rank
                    best_objective = objective
                    best_quality = candidate_quality
                    best_quality_evaluation_index = evaluation_count
                    current_state = candidate_state
                    best_metadata = {
                        root: (source, weight, candidate_count_by_root[root])
                        for root in component_roots
                    }
                else:
                    set_state(current_state)

        coordinate_candidate_count = 0
        completed_passes = 0
        for _pass in range(int(endpoint["coordinate_descent_passes"])):
            completed_passes += 1
            for root in component_roots:
                root_best_state = dict(current_state)
                root_best_rank = best_rank
                root_best_objective = best_objective
                root_best_metadata = best_metadata[root]
                for candidate, source, weight in candidates_by_root[root]:
                    if direction_key(candidate) == direction_key(current_state[root]):
                        continue
                    coordinate_candidate_count += 1
                    candidate_state = dict(current_state)
                    candidate_state[root] = candidate
                    candidate_quality, objective = evaluate(
                        component_roots, candidate_state
                    )
                    rank = (
                        objective,
                        tuple(
                            direction_key(candidate_state[item])
                            for item in component_roots
                        ),
                    )
                    if rank < root_best_rank:
                        root_best_rank = rank
                        root_best_objective = objective
                        best_quality = candidate_quality
                        best_quality_evaluation_index = evaluation_count
                        root_best_state = candidate_state
                        root_best_metadata = (
                            source,
                            weight,
                            candidate_count_by_root[root],
                        )
                    else:
                        set_state(root_best_state)
                current_state = root_best_state
                best_rank = root_best_rank
                best_objective = root_best_objective
                best_metadata[root] = root_best_metadata
                set_state(current_state)

        static_exit_quality: dict[str, Any] | None = None
        static_exit_objective: tuple[float, ...] | None = None
        static_exit_objective_sha256: str | None = None
        static_exit_coordinate_sha256: str | None = None
        static_exit_quality_evaluation_index: int | None = None
        component_sha = str(
            stable_component[
                "interaction_component_sha256"
                if pattern_endpoint
                else "component_sha256"
            ]
        )
        component_source_triangle_sha256 = sorted(
            {str(triangle_stable_ids[triangle]) for triangle in component_triangles}
        )
        if fine_refinement_endpoint:
            # The phase-1 exit is a full, reproducible component query.  Every
            # component reaches this point before the fine eligibility gate.
            static_exit_quality, static_exit_objective = evaluate(
                component_roots, current_state
            )
            if tuple(static_exit_objective) != tuple(best_objective):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free fine static exit objective is not reproducible"
                )
            best_quality = static_exit_quality
            best_quality_evaluation_index = evaluation_count
            static_exit_quality_evaluation_index = evaluation_count
            static_exit_coordinate_sha256 = coordinate_state_sha256(
                component_roots
            )
            static_exit_objective_sha256 = _canonical_hash(
                {
                    "quality_objective_fields": list(objective_fields),
                    "objective": [
                        float(value) for value in static_exit_objective
                    ],
                }
            )
            fine_static_component_order.append(component_sha)
            fine_component_static_evidence.append(
                {
                    "interaction_component_sha256": component_sha,
                    "static_exit_status": str(static_exit_quality["status"]),
                    "static_exit_quality_sha256": str(
                        static_exit_quality["quality_sha256"]
                    ),
                    "static_exit_objective_sha256": (
                        static_exit_objective_sha256
                    ),
                    "static_exit_coordinate_sha256": (
                        static_exit_coordinate_sha256
                    ),
                    "static_exit_quality_evaluation_index": (
                        static_exit_quality_evaluation_index
                    ),
                }
            )
            fine_component_contexts[component_sha] = {
                "component_roots": component_roots,
                "component_triangles": component_triangles,
                "component_edges": component_edges,
                "current_state": dict(current_state),
                "best_rank": best_rank,
                "best_objective": best_objective,
                "best_quality": best_quality,
                "best_quality_evaluation_index": (
                    best_quality_evaluation_index
                ),
                "best_metadata": dict(best_metadata),
            }

        objective_by_field = dict(zip(objective_fields, best_objective))
        if len(objective_by_field) != len(objective_fields):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free quality objective result is incomplete"
            )
        quality_incomplete = any(
            objective_by_field[field] > 0.0
            for field in _OWNER_FREE_QUALITY_COUNT_FIELDS
        )
        if (
            pattern_steps
            and quality_incomplete
            and not fine_refinement_endpoint
        ):
            pattern_refined_component_count += 1
            tangent_weight = float(endpoint["tangent_interior_weight"])
            pair_moves = endpoint.get("shared_triangle_pair_moves") is True
            pair_candidate_cap = int(
                endpoint["maximum_pair_candidates_per_edge_step"]
            )
            def tangent_options(
                root: int, step: float
            ) -> list[tuple[float, float, float]]:
                nonlocal rejected_margin_candidate_count
                direction = _unit_vector(current_state[root])
                if direction is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free pattern direction is zero"
                    )
                axes = (
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0),
                )
                reference = min(
                    axes, key=lambda axis: abs(_vector_dot(direction, axis))
                )
                first_basis = _unit_vector(_vector_cross(direction, reference))
                if first_basis is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free tangent basis is degenerate"
                    )
                second_basis = _unit_vector(
                    _vector_cross(direction, first_basis)
                )
                if second_basis is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free second tangent basis is degenerate"
                    )
                raw_targets = [
                    _vector_add(
                        _vector_scale(direction, math.cos(step)),
                        _vector_scale(basis, sign * math.sin(step)),
                    )
                    for basis in (first_basis, second_basis)
                    for sign in (-1.0, 1.0)
                ]
                unique: dict[
                    tuple[float, float, float], tuple[float, float, float]
                ] = {}
                for target in raw_targets:
                    candidate, margin, _alignment, _candidate_count = (
                        _closest_feasible_cone_direction(
                        target,
                        incident_normals[root],
                        direction,
                        interior_weight=tangent_weight,
                        )
                    )
                    if margin < required_margin_for_candidate(root, candidate):
                        rejected_margin_candidate_count += 1
                        continue
                    unique[direction_key(candidate)] = candidate
                return [unique[key] for key in sorted(unique)]

            for step in pattern_steps:
                for _pattern_pass in range(
                    int(endpoint["tangent_coordinate_passes_per_step"])
                ):
                    for root in component_roots:
                        root_best_state = dict(current_state)
                        root_best_rank = best_rank
                        root_best_objective = best_objective
                        for candidate in tangent_options(root, step):
                            if direction_key(candidate) == direction_key(
                                current_state[root]
                            ):
                                continue
                            candidate_count_by_root[root] += 1
                            candidate_state = dict(current_state)
                            candidate_state[root] = candidate
                            _quality, objective = evaluate(
                                component_roots, candidate_state
                            )
                            pattern_evaluation_count += 1
                            rank = (
                                objective,
                                tuple(
                                    direction_key(candidate_state[item])
                                    for item in component_roots
                                ),
                            )
                            if rank < root_best_rank:
                                root_best_rank = rank
                                root_best_objective = objective
                                root_best_state = candidate_state
                            else:
                                set_state(root_best_state)
                        if root_best_rank < best_rank:
                            pattern_accepted_move_count += 1
                            best_metadata[root] = (
                                "tangent_pattern",
                                tangent_weight,
                                candidate_count_by_root[root],
                            )
                        current_state = root_best_state
                        best_rank = root_best_rank
                        best_objective = root_best_objective
                        set_state(current_state)

                    if pair_moves:
                        for first_root, second_root in component_edges:
                            first_options = tangent_options(first_root, step)
                            second_options = tangent_options(second_root, step)
                            pair_candidates = [
                                (first_candidate, second_candidate)
                                for first_candidate in first_options
                                for second_candidate in second_options
                            ]
                            if len(pair_candidates) > pair_candidate_cap:
                                raise BoundaryLayerSmokeError(
                                    "owner-free pair candidate cap was exceeded"
                                )
                            pair_best_state = dict(current_state)
                            pair_best_rank = best_rank
                            pair_best_objective = best_objective
                            for first_candidate, second_candidate in pair_candidates:
                                candidate_count_by_root[first_root] += 1
                                candidate_count_by_root[second_root] += 1
                                candidate_state = dict(current_state)
                                candidate_state[first_root] = first_candidate
                                candidate_state[second_root] = second_candidate
                                _quality, objective = evaluate(
                                    component_roots, candidate_state
                                )
                                pattern_evaluation_count += 1
                                rank = (
                                    objective,
                                    tuple(
                                        direction_key(candidate_state[item])
                                        for item in component_roots
                                    ),
                                )
                                if rank < pair_best_rank:
                                    pair_best_rank = rank
                                    pair_best_objective = objective
                                    pair_best_state = candidate_state
                                else:
                                    set_state(pair_best_state)
                            if pair_best_rank < best_rank:
                                pattern_accepted_move_count += 1
                                best_metadata[first_root] = (
                                    "source_triangle_edge_pair_pattern",
                                    tangent_weight,
                                    candidate_count_by_root[first_root],
                                )
                                best_metadata[second_root] = (
                                    "source_triangle_edge_pair_pattern",
                                    tangent_weight,
                                    candidate_count_by_root[second_root],
                                )
                            current_state = pair_best_state
                            best_rank = pair_best_rank
                            best_objective = pair_best_objective
                            set_state(current_state)

        search_exit_quality_sha256: str | None = None
        search_exit_coordinate_sha256: str | None = None
        if pattern_endpoint:
            if fine_refinement_endpoint:
                if (
                    static_exit_quality is None
                    or static_exit_objective is None
                    or static_exit_coordinate_sha256 is None
                ):
                    set_state(original)
                    raise BoundaryLayerSmokeError(
                        "owner-free fine static exit evidence is incomplete"
                    )
                exit_quality = static_exit_quality
                exit_objective = static_exit_objective
            else:
                exit_quality, exit_objective = evaluate(
                    component_roots, current_state
                )
                if tuple(exit_objective) != tuple(best_objective):
                    set_state(original)
                    raise BoundaryLayerSmokeError(
                        "owner-free component exit objective is not reproducible"
                    )
            search_exit_quality_sha256 = str(exit_quality["quality_sha256"])
            search_exit_coordinate_sha256 = (
                static_exit_coordinate_sha256
                if fine_refinement_endpoint
                else coordinate_state_sha256(component_roots)
            )

        selected_state.update(current_state)
        selected_metadata.update(best_metadata)
        component_record: dict[str, Any] = {
                (
                    "interaction_component_sha256"
                    if pattern_endpoint
                    else "component_sha256"
                ): str(
                    stable_component[
                        "interaction_component_sha256"
                        if pattern_endpoint
                        else "component_sha256"
                    ]
                ),
                "candidate_root_count": len(component_roots),
                "joint_candidate_count": joint_candidate_count,
                "coordinate_candidate_count": coordinate_candidate_count,
                "quality_evaluation_count": evaluation_count
                - component_start_evaluations,
                "selected_objective": [float(value) for value in best_objective],
            }
        if pattern_endpoint:
            component_record.update(
                {
                    "search_exit_quality_sha256": search_exit_quality_sha256,
                    "search_exit_coordinate_sha256": (
                        search_exit_coordinate_sha256
                    ),
                }
            )
        if fine_refinement_endpoint:
            if (
                static_exit_quality is None
                or static_exit_objective is None
                or static_exit_objective_sha256 is None
                or static_exit_coordinate_sha256 is None
                or static_exit_quality_evaluation_index is None
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free fine static component record is incomplete"
                )
            component_record.update(
                {
                    "source_triangle_count": len(
                        component_source_triangle_sha256
                    ),
                    "source_triangle_sha256": (
                        component_source_triangle_sha256
                    ),
                    "static_exit_quality": static_exit_quality,
                    "static_exit_quality_sha256": str(
                        static_exit_quality["quality_sha256"]
                    ),
                    "static_exit_objective": [
                        float(value) for value in static_exit_objective
                    ],
                    "static_exit_objective_sha256": (
                        static_exit_objective_sha256
                    ),
                    "static_exit_coordinate_sha256": (
                        static_exit_coordinate_sha256
                    ),
                    "static_exit_status": str(static_exit_quality["status"]),
                    "static_exit_quality_evaluation_index": (
                        static_exit_quality_evaluation_index
                    ),
                }
            )
        component_records.append(component_record)
        if fine_refinement_endpoint:
            fine_component_contexts[component_sha]["component_record"] = (
                component_record
            )

    if fine_refinement_endpoint:
        static_quality_evaluation_count = evaluation_count
        if (
            static_quality_evaluation_count
            > _FINE_REFINEMENT_STATIC_QUALITY_EVALUATION_CAP
        ):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free fine static search exceeds its conservative cap"
            )
        failing_component_ids = [
            component_sha
            for component_sha in fine_static_component_order
            if str(
                fine_component_contexts[component_sha]["best_quality"]["status"]
            )
            != "PASS"
        ]
        maximum_refined_component_count = int(
            endpoint.get("maximum_refined_component_count", 0)
        )
        required_refined_root_count = int(
            endpoint.get("refined_component_root_count", 0)
        )
        required_refined_triangle_count = int(
            endpoint.get("refined_component_source_triangle_count", 0)
        )
        if (
            maximum_refined_component_count != 1
            or required_refined_root_count != 3
            or required_refined_triangle_count != 1
            or len(failing_component_ids) > maximum_refined_component_count
        ):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free fine refinement static failure gate rejected the scope"
            )

        selected_component_evidence: dict[str, Any] | None = None
        prefix_checkpoint: dict[str, Any] | None = None
        fine_component_order: list[str] = []
        first_fine_quality_evaluation_index: int | None = None
        first_tail_quality_evaluation_index: int | None = None
        tail_start_coordinate_sha256: str | None = None
        tail_start_direction_state_sha256: str | None = None
        tail_start_direction_field_sha256: str | None = None
        tail_start_quality_sha256: str | None = None
        tail_started_from_prefix_checkpoint = False
        fine_evaluation_start = evaluation_count
        prefix_steps = [float(2.0**-exponent) for exponent in range(1, 6)]
        tail_steps = [float(2.0**-exponent) for exponent in range(6, 13)]
        if pattern_steps != [
            float(2.0**-exponent) for exponent in range(1, 13)
        ]:
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free fine refinement tangent steps differ"
            )

        def fine_direction_state_sha256(
            roots: Sequence[int], state: Mapping[int, Sequence[float]]
        ) -> str:
            records = []
            for root in sorted(
                {int(value) for value in roots},
                key=lambda value: _stable_root_id(root_coordinates[value]),
            ):
                direction = _unit_vector(state[root])
                if direction is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free fine direction state contains a zero vector"
                    )
                records.append(
                    {
                        "root_coordinate_sha256": _stable_root_id(
                            root_coordinates[root]
                        ),
                        "direction_float_hex": [
                            float(value).hex() for value in direction
                        ],
                    }
                )
            return _canonical_hash(
                {
                    "encoding": "python-float-hex/root-sha/direction-v1",
                    "records": records,
                }
            )

        if failing_component_ids:
            selected_component_sha = failing_component_ids[0]
            context = fine_component_contexts[selected_component_sha]
            component_roots = tuple(context["component_roots"])
            component_triangles = list(context["component_triangles"])
            component_edges = list(context["component_edges"])
            component_triangle_ids = sorted(
                {str(triangle_stable_ids[item]) for item in component_triangles}
            )
            if (
                len(component_roots) != required_refined_root_count
                or len(component_triangles) != required_refined_triangle_count
                or len(component_triangle_ids)
                != required_refined_triangle_count
                or len(component_edges) != 3
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free fine refinement selected component has the wrong shape"
                )
            selected_component_evidence = {
                "interaction_component_sha256": selected_component_sha,
                "candidate_root_count": len(component_roots),
                "root_coordinate_sha256": sorted(
                    _stable_root_id(root_coordinates[root])
                    for root in component_roots
                ),
                "source_triangle_count": len(component_triangle_ids),
                "source_triangle_sha256": component_triangle_ids,
                "unique_source_triangle_edge_count": len(component_edges),
            }
            fine_component_order.append(selected_component_sha)
            pattern_refined_component_count = 1
            current_state = dict(context["current_state"])
            best_rank = context["best_rank"]
            best_objective = tuple(context["best_objective"])
            best_quality = dict(context["best_quality"])
            best_quality_evaluation_index = int(
                context["best_quality_evaluation_index"]
            )
            best_metadata = dict(context["best_metadata"])
            tangent_weight = float(endpoint["tangent_interior_weight"])
            pair_candidate_cap = int(
                endpoint["maximum_pair_candidates_per_edge_step"]
            )
            pattern_passes = int(
                endpoint["tangent_coordinate_passes_per_step"]
            )
            fine_pattern_upper_bound = (
                len(pattern_steps)
                * pattern_passes
                * (
                    4 * len(component_roots)
                    + pair_candidate_cap * len(component_edges)
                )
            )
            triple_pattern_upper_bound = 0
            if triple_refinement_endpoint:
                triple_pattern_upper_bound = (
                    len(pattern_steps)
                    * int(endpoint["triple_coordinate_passes_per_step"])
                    * int(
                        endpoint[
                            "atomic_triple_cartesian_candidates_per_step_pass"
                        ]
                    )
                )
                if (
                    triple_pattern_upper_bound
                    != _TRIPLE_REFINEMENT_QUALITY_EVALUATION_CAP
                ):
                    set_state(original)
                    raise BoundaryLayerSmokeError(
                        "owner-free triple refinement budget differs"
                    )
            replay_and_final_reserve = 6 + len(runtime_components)
            if (
                evaluation_count
                + fine_pattern_upper_bound
                + triple_pattern_upper_bound
                + replay_and_final_reserve
                > maximum_evaluations
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free fine refinement budget is insufficient"
                )

            def fine_tangent_options(
                root: int, step: float
            ) -> list[tuple[float, float, float]]:
                nonlocal rejected_margin_candidate_count
                direction = _unit_vector(current_state[root])
                if direction is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free fine pattern direction is zero"
                    )
                axes = (
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, 0.0, 1.0),
                )
                reference = min(
                    axes, key=lambda axis: abs(_vector_dot(direction, axis))
                )
                first_basis = _unit_vector(_vector_cross(direction, reference))
                if first_basis is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free fine tangent basis is degenerate"
                    )
                second_basis = _unit_vector(
                    _vector_cross(direction, first_basis)
                )
                if second_basis is None:
                    raise BoundaryLayerSmokeError(
                        "owner-free fine second tangent basis is degenerate"
                    )
                raw_targets = [
                    _vector_add(
                        _vector_scale(direction, math.cos(step)),
                        _vector_scale(basis, sign * math.sin(step)),
                    )
                    for basis in (first_basis, second_basis)
                    for sign in (-1.0, 1.0)
                ]
                unique: dict[
                    tuple[float, float, float], tuple[float, float, float]
                ] = {}
                for target in raw_targets:
                    candidate, margin, _alignment, _candidate_count = (
                        _closest_feasible_cone_direction(
                            target,
                            incident_normals[root],
                            direction,
                            interior_weight=tangent_weight,
                        )
                    )
                    if margin < required_margin_for_candidate(root, candidate):
                        rejected_margin_candidate_count += 1
                        continue
                    unique[direction_key(candidate)] = candidate
                return [unique[key] for key in sorted(unique)]

            set_state(current_state)
            for step_index, step in enumerate(pattern_steps):
                if step_index == len(prefix_steps):
                    if prefix_checkpoint is None:
                        set_state(original)
                        raise BoundaryLayerSmokeError(
                            "owner-free fine prefix checkpoint is missing"
                        )
                    tail_start_coordinate_sha256 = coordinate_state_sha256(
                        component_roots
                    )
                    tail_start_direction_state_sha256 = (
                        fine_direction_state_sha256(
                            component_roots, current_state
                        )
                    )
                    tail_start_direction_field_sha256 = (
                        _stable_direction_field_sha256(
                            roots=component_roots,
                            root_coordinates=root_coordinates,
                            directions=current_state,
                        )
                    )
                    tail_start_quality_sha256 = str(
                        best_quality["quality_sha256"]
                    )
                    tail_started_from_prefix_checkpoint = (
                        tail_start_coordinate_sha256
                        == prefix_checkpoint[
                            "selected_state_coordinate_sha256"
                        ]
                        and tail_start_direction_state_sha256
                        == prefix_checkpoint[
                            "selected_direction_state_sha256"
                        ]
                        and tail_start_direction_field_sha256
                        == prefix_checkpoint[
                            "selected_direction_field_sha256"
                        ]
                        and tail_start_quality_sha256
                        == prefix_checkpoint[
                            "selected_state_quality_sha256"
                        ]
                    )
                    if not tail_started_from_prefix_checkpoint:
                        set_state(original)
                        raise BoundaryLayerSmokeError(
                            "owner-free fine tail did not continue from prefix"
                        )
                for _pattern_pass in range(pattern_passes):
                    for root in component_roots:
                        root_best_state = dict(current_state)
                        root_best_rank = best_rank
                        root_best_objective = best_objective
                        root_best_quality = best_quality
                        root_best_quality_evaluation_index = (
                            best_quality_evaluation_index
                        )
                        for candidate in fine_tangent_options(root, step):
                            if direction_key(candidate) == direction_key(
                                current_state[root]
                            ):
                                continue
                            candidate_count_by_root[root] += 1
                            candidate_state = dict(current_state)
                            candidate_state[root] = candidate
                            candidate_quality, objective = evaluate(
                                component_roots, candidate_state
                            )
                            if first_fine_quality_evaluation_index is None:
                                first_fine_quality_evaluation_index = (
                                    evaluation_count
                                )
                            if (
                                step_index >= len(prefix_steps)
                                and first_tail_quality_evaluation_index is None
                            ):
                                first_tail_quality_evaluation_index = (
                                    evaluation_count
                                )
                            pattern_evaluation_count += 1
                            rank = (
                                objective,
                                tuple(
                                    direction_key(candidate_state[item])
                                    for item in component_roots
                                ),
                            )
                            if rank < root_best_rank:
                                root_best_rank = rank
                                root_best_objective = objective
                                root_best_quality = candidate_quality
                                root_best_quality_evaluation_index = (
                                    evaluation_count
                                )
                                root_best_state = candidate_state
                            else:
                                set_state(root_best_state)
                        if root_best_rank < best_rank:
                            pattern_accepted_move_count += 1
                            best_metadata[root] = (
                                "tangent_pattern",
                                tangent_weight,
                                candidate_count_by_root[root],
                            )
                        current_state = root_best_state
                        best_rank = root_best_rank
                        best_objective = root_best_objective
                        best_quality = root_best_quality
                        best_quality_evaluation_index = (
                            root_best_quality_evaluation_index
                        )
                        set_state(current_state)

                    for first_root, second_root in component_edges:
                        first_options = fine_tangent_options(first_root, step)
                        second_options = fine_tangent_options(second_root, step)
                        pair_candidates = [
                            (first_candidate, second_candidate)
                            for first_candidate in first_options
                            for second_candidate in second_options
                        ]
                        if len(pair_candidates) > pair_candidate_cap:
                            set_state(original)
                            raise BoundaryLayerSmokeError(
                                "owner-free fine pair candidate cap was exceeded"
                            )
                        pair_best_state = dict(current_state)
                        pair_best_rank = best_rank
                        pair_best_objective = best_objective
                        pair_best_quality = best_quality
                        pair_best_quality_evaluation_index = (
                            best_quality_evaluation_index
                        )
                        for first_candidate, second_candidate in pair_candidates:
                            candidate_count_by_root[first_root] += 1
                            candidate_count_by_root[second_root] += 1
                            candidate_state = dict(current_state)
                            candidate_state[first_root] = first_candidate
                            candidate_state[second_root] = second_candidate
                            candidate_quality, objective = evaluate(
                                component_roots, candidate_state
                            )
                            if first_fine_quality_evaluation_index is None:
                                first_fine_quality_evaluation_index = (
                                    evaluation_count
                                )
                            if (
                                step_index >= len(prefix_steps)
                                and first_tail_quality_evaluation_index is None
                            ):
                                first_tail_quality_evaluation_index = (
                                    evaluation_count
                                )
                            pattern_evaluation_count += 1
                            rank = (
                                objective,
                                tuple(
                                    direction_key(candidate_state[item])
                                    for item in component_roots
                                ),
                            )
                            if rank < pair_best_rank:
                                pair_best_rank = rank
                                pair_best_objective = objective
                                pair_best_quality = candidate_quality
                                pair_best_quality_evaluation_index = (
                                    evaluation_count
                                )
                                pair_best_state = candidate_state
                            else:
                                set_state(pair_best_state)
                        if pair_best_rank < best_rank:
                            pattern_accepted_move_count += 1
                            best_metadata[first_root] = (
                                "source_triangle_edge_pair_pattern",
                                tangent_weight,
                                candidate_count_by_root[first_root],
                            )
                            best_metadata[second_root] = (
                                "source_triangle_edge_pair_pattern",
                                tangent_weight,
                                candidate_count_by_root[second_root],
                            )
                        current_state = pair_best_state
                        best_rank = pair_best_rank
                        best_objective = pair_best_objective
                        best_quality = pair_best_quality
                        best_quality_evaluation_index = (
                            pair_best_quality_evaluation_index
                        )
                        set_state(current_state)

                if step_index == len(prefix_steps) - 1:
                    prefix_objective = [
                        float(value) for value in best_objective
                    ]
                    prefix_objective_sha256 = _canonical_hash(
                        {
                            "quality_objective_fields": list(objective_fields),
                            "objective": prefix_objective,
                        }
                    )
                    prefix_direction_state_sha256 = (
                        fine_direction_state_sha256(
                            component_roots, current_state
                        )
                    )
                    prefix_direction_field_sha256 = (
                        _stable_direction_field_sha256(
                            roots=component_roots,
                            root_coordinates=root_coordinates,
                            directions=current_state,
                        )
                    )
                    prefix_checkpoint = {
                        "selected_component_sha256": selected_component_sha,
                        "prefix_steps_radians": prefix_steps,
                        "selected_state_coordinate_sha256": (
                            coordinate_state_sha256(component_roots)
                        ),
                        "selected_state_quality": best_quality,
                        "selected_state_quality_sha256": str(
                            best_quality["quality_sha256"]
                        ),
                        "selected_state_objective": prefix_objective,
                        "selected_state_objective_sha256": (
                            prefix_objective_sha256
                        ),
                        "selected_direction_state_sha256": (
                            prefix_direction_state_sha256
                        ),
                        "selected_direction_field_sha256": (
                            prefix_direction_field_sha256
                        ),
                        "quality_evaluation_index": (
                            evaluation_count
                        ),
                    }

            if (
                prefix_checkpoint is None
                or not tail_started_from_prefix_checkpoint
                or first_tail_quality_evaluation_index is None
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free fine prefix/tail evidence is incomplete"
                )
            selected_state.update(current_state)
            selected_metadata.update(best_metadata)
            selected_record = context["component_record"]
            selected_record["quality_evaluation_count"] = (
                int(selected_record["quality_evaluation_count"])
                + evaluation_count
                - fine_evaluation_start
            )
            selected_record["selected_objective"] = [
                float(value) for value in best_objective
            ]
            selected_record["search_exit_quality_sha256"] = str(
                best_quality["quality_sha256"]
            )
            selected_record["search_exit_coordinate_sha256"] = (
                coordinate_state_sha256(component_roots)
            )

        fine_quality_evaluation_count = evaluation_count - fine_evaluation_start
        fine_handoff_quality_evaluation_count = evaluation_count
        fine_rejected_margin_candidate_count = rejected_margin_candidate_count

        if triple_refinement_endpoint:
            triple_root_candidate_cap = int(
                endpoint["tangent_candidates_per_root_per_step"]
            )
            triple_atomic_candidate_cap = int(
                endpoint[
                    "atomic_triple_cartesian_candidates_per_step_pass"
                ]
            )
            triple_pattern_passes = int(
                endpoint["triple_coordinate_passes_per_step"]
            )
            triple_required_root_count = int(
                endpoint["triple_refinement_root_count"]
            )
            if (
                endpoint.get(
                    "static_component_searches_complete_before_fine_refinement"
                )
                is not True
                or endpoint.get(
                    "single_and_pair_phases_complete_before_triple_refinement"
                )
                is not True
                or endpoint.get("triple_refinement_enabled") is not True
                or triple_required_root_count != 3
                or triple_root_candidate_cap != 4
                or triple_atomic_candidate_cap != 64
                or triple_pattern_passes != 2
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free triple refinement endpoint is inconsistent"
                )

            triple_handoff: dict[str, Any] | None = None
            triple_final_state: dict[str, Any] | None = None
            triple_evaluation_start = evaluation_count
            first_triple_quality_evaluation_index: int | None = None
            last_triple_quality_evaluation_index: int | None = None

            def triple_objective_sha256(
                objective: Sequence[float],
            ) -> str:
                return _canonical_hash(
                    {
                        "quality_objective_fields": list(objective_fields),
                        "objective": [float(value) for value in objective],
                    }
                )

            def triple_checkpoint(
                roots: Sequence[int],
                state: Mapping[int, Sequence[float]],
                quality: Mapping[str, Any],
                objective: Sequence[float],
            ) -> dict[str, Any]:
                return {
                    "selected_state_coordinate_sha256": (
                        coordinate_state_sha256(roots)
                    ),
                    "selected_state_quality": dict(quality),
                    "selected_state_quality_sha256": str(
                        quality["quality_sha256"]
                    ),
                    "selected_state_objective": [
                        float(value) for value in objective
                    ],
                    "selected_state_objective_sha256": (
                        triple_objective_sha256(objective)
                    ),
                    "selected_direction_state_sha256": (
                        fine_direction_state_sha256(roots, state)
                    ),
                    "selected_direction_field_sha256": (
                        _stable_direction_field_sha256(
                            roots=roots,
                            root_coordinates=root_coordinates,
                            directions=state,
                        )
                    ),
                }

            if failing_component_ids:
                # The single/pair fine state is the only triple entry.  There
                # is intentionally no set_state call between these hashes and
                # the first atomic Cartesian evaluation.
                fine_exit_checkpoint = triple_checkpoint(
                    component_roots,
                    current_state,
                    best_quality,
                    best_objective,
                )
                selected_record["fine_exit_checkpoint"] = {
                    **fine_exit_checkpoint,
                    "selected_directions": [
                        {
                            "root_coordinate_sha256": _stable_root_id(
                                root_coordinates[root]
                            ),
                            "direction_float_hex": [
                                float(value).hex()
                                for value in (
                                    _unit_vector(current_state[root])
                                    or original[root]
                                )
                            ],
                        }
                        for root in sorted(
                            component_roots,
                            key=lambda value: _stable_root_id(
                                root_coordinates[value]
                            ),
                        )
                    ],
                    "fine_exit_quality_evaluation_index": (
                        fine_handoff_quality_evaluation_count
                    ),
                }
                triple_entry_checkpoint = triple_checkpoint(
                    component_roots,
                    current_state,
                    best_quality,
                    best_objective,
                )
                triple_handoff = {
                    "selected_component_sha256": selected_component_sha,
                    "fine_exit_coordinate_sha256": fine_exit_checkpoint[
                        "selected_state_coordinate_sha256"
                    ],
                    "triple_entry_coordinate_sha256": triple_entry_checkpoint[
                        "selected_state_coordinate_sha256"
                    ],
                    "fine_exit_quality_sha256": fine_exit_checkpoint[
                        "selected_state_quality_sha256"
                    ],
                    "fine_exit_quality": fine_exit_checkpoint[
                        "selected_state_quality"
                    ],
                    "triple_entry_quality_sha256": triple_entry_checkpoint[
                        "selected_state_quality_sha256"
                    ],
                    "fine_exit_objective_sha256": fine_exit_checkpoint[
                        "selected_state_objective_sha256"
                    ],
                    "fine_exit_objective": fine_exit_checkpoint[
                        "selected_state_objective"
                    ],
                    "triple_entry_objective_sha256": triple_entry_checkpoint[
                        "selected_state_objective_sha256"
                    ],
                    "fine_exit_direction_state_sha256": fine_exit_checkpoint[
                        "selected_direction_state_sha256"
                    ],
                    "triple_entry_direction_state_sha256": (
                        triple_entry_checkpoint[
                            "selected_direction_state_sha256"
                        ]
                    ),
                    "fine_exit_direction_field_sha256": fine_exit_checkpoint[
                        "selected_direction_field_sha256"
                    ],
                    "triple_entry_direction_field_sha256": (
                        triple_entry_checkpoint[
                            "selected_direction_field_sha256"
                        ]
                    ),
                    "fine_exit_quality_evaluation_index": (
                        fine_handoff_quality_evaluation_count
                    ),
                    "triple_quality_evaluation_start_index_exclusive": (
                        triple_evaluation_start
                    ),
                    "without_reset": True,
                }
                if any(
                    triple_handoff[f"fine_exit_{suffix}"]
                    != triple_handoff[f"triple_entry_{suffix}"]
                    for suffix in (
                        "coordinate_sha256",
                        "quality_sha256",
                        "objective_sha256",
                        "direction_state_sha256",
                        "direction_field_sha256",
                    )
                ):
                    set_state(original)
                    raise BoundaryLayerSmokeError(
                        "owner-free triple refinement did not inherit the fine state"
                    )

                for step_index, step in enumerate(pattern_steps, start=1):
                    for pass_index in range(1, triple_pattern_passes + 1):
                        pass_entry = triple_checkpoint(
                            component_roots,
                            current_state,
                            best_quality,
                            best_objective,
                        )
                        pass_evaluation_start = evaluation_count
                        rejected_before = rejected_margin_candidate_count
                        options_by_root: dict[
                            int, list[tuple[float, float, float]]
                        ] = {}
                        for root in component_roots:
                            options = [
                                candidate
                                for candidate in fine_tangent_options(root, step)
                                if direction_key(candidate)
                                != direction_key(current_state[root])
                            ]
                            if len(options) > triple_root_candidate_cap:
                                set_state(original)
                                raise BoundaryLayerSmokeError(
                                    "owner-free triple root candidate cap was exceeded"
                                )
                            options_by_root[root] = options

                        root_candidate_counts = [
                            {
                                "root_coordinate_sha256": _stable_root_id(
                                    root_coordinates[root]
                                ),
                                "candidate_count": len(options_by_root[root]),
                            }
                            for root in sorted(
                                component_roots,
                                key=lambda value: _stable_root_id(
                                    root_coordinates[value]
                                ),
                            )
                        ]
                        first_root, second_root, third_root = component_roots
                        atomic_candidates = [
                            (first, second, third)
                            for first in options_by_root[first_root]
                            for second in options_by_root[second_root]
                            for third in options_by_root[third_root]
                        ]
                        if len(atomic_candidates) > triple_atomic_candidate_cap:
                            set_state(original)
                            raise BoundaryLayerSmokeError(
                                "owner-free atomic triple candidate cap was exceeded"
                            )

                        atomic_best_state = dict(current_state)
                        atomic_best_rank = best_rank
                        atomic_best_objective = best_objective
                        atomic_best_quality = best_quality
                        atomic_best_quality_evaluation_index = (
                            best_quality_evaluation_index
                        )
                        for first, second, third in atomic_candidates:
                            candidate_state = dict(current_state)
                            candidate_state[first_root] = first
                            candidate_state[second_root] = second
                            candidate_state[third_root] = third
                            candidate_quality, objective = evaluate(
                                component_roots, candidate_state
                            )
                            triple_quality_evaluation_count += 1
                            for root in component_roots:
                                candidate_count_by_root[root] += 1
                            if first_triple_quality_evaluation_index is None:
                                first_triple_quality_evaluation_index = (
                                    evaluation_count
                                )
                            last_triple_quality_evaluation_index = evaluation_count
                            rank = (
                                objective,
                                tuple(
                                    direction_key(candidate_state[root])
                                    for root in component_roots
                                ),
                            )
                            if rank < atomic_best_rank:
                                atomic_best_state = candidate_state
                                atomic_best_rank = rank
                                atomic_best_objective = objective
                                atomic_best_quality = candidate_quality
                                atomic_best_quality_evaluation_index = (
                                    evaluation_count
                                )
                            else:
                                set_state(atomic_best_state)

                        accepted_move = atomic_best_rank < best_rank
                        if accepted_move:
                            triple_accepted_move_count += 1
                            for root in component_roots:
                                best_metadata[root] = (
                                    "tangent_pattern",
                                    tangent_weight,
                                    candidate_count_by_root[root],
                                )
                        current_state = atomic_best_state
                        best_rank = atomic_best_rank
                        best_objective = atomic_best_objective
                        best_quality = atomic_best_quality
                        best_quality_evaluation_index = (
                            atomic_best_quality_evaluation_index
                        )
                        set_state(current_state)
                        pass_exit = triple_checkpoint(
                            component_roots,
                            current_state,
                            best_quality,
                            best_objective,
                        )
                        pass_evaluation_count = (
                            evaluation_count - pass_evaluation_start
                        )
                        triple_step_pass_records.append(
                            {
                                "step_index_1_based": step_index,
                                "pass_index_1_based": pass_index,
                                "step_radians": float(step),
                                "step_radians_float_hex": float(step).hex(),
                                "root_candidate_counts": root_candidate_counts,
                                "candidate_upper_bound": (
                                    triple_atomic_candidate_cap
                                ),
                                "atomic_candidate_count": len(
                                    atomic_candidates
                                ),
                                "rejected_margin_candidate_count": (
                                    rejected_margin_candidate_count
                                    - rejected_before
                                ),
                                "quality_evaluation_start_index_exclusive": (
                                    pass_evaluation_start
                                ),
                                "first_quality_evaluation_index": (
                                    pass_evaluation_start + 1
                                    if pass_evaluation_count
                                    else None
                                ),
                                "last_quality_evaluation_index": (
                                    evaluation_count
                                    if pass_evaluation_count
                                    else None
                                ),
                                "quality_evaluation_end_index_inclusive": (
                                    evaluation_count
                                ),
                                "quality_evaluation_count": (
                                    pass_evaluation_count
                                ),
                                "accepted_move": accepted_move,
                                "entry_coordinate_sha256": pass_entry[
                                    "selected_state_coordinate_sha256"
                                ],
                                "exit_coordinate_sha256": pass_exit[
                                    "selected_state_coordinate_sha256"
                                ],
                                "entry_quality_sha256": pass_entry[
                                    "selected_state_quality_sha256"
                                ],
                                "exit_quality_sha256": pass_exit[
                                    "selected_state_quality_sha256"
                                ],
                                "entry_objective_sha256": pass_entry[
                                    "selected_state_objective_sha256"
                                ],
                                "exit_objective_sha256": pass_exit[
                                    "selected_state_objective_sha256"
                                ],
                                "entry_direction_state_sha256": pass_entry[
                                    "selected_direction_state_sha256"
                                ],
                                "exit_direction_state_sha256": pass_exit[
                                    "selected_direction_state_sha256"
                                ],
                                "entry_direction_field_sha256": pass_entry[
                                    "selected_direction_field_sha256"
                                ],
                                "exit_direction_field_sha256": pass_exit[
                                    "selected_direction_field_sha256"
                                ],
                            }
                        )

                expected_pass_record_count = (
                    len(pattern_steps) * triple_pattern_passes
                )
                if (
                    len(triple_step_pass_records)
                    != expected_pass_record_count
                    or triple_quality_evaluation_count
                    > _TRIPLE_REFINEMENT_QUALITY_EVALUATION_CAP
                    or triple_quality_evaluation_count
                    != evaluation_count - triple_evaluation_start
                ):
                    set_state(original)
                    raise BoundaryLayerSmokeError(
                        "owner-free triple refinement evidence exceeds its scope"
                    )
                triple_rejected_margin_candidate_count = (
                    rejected_margin_candidate_count
                    - fine_rejected_margin_candidate_count
                )
                selected_state.update(current_state)
                selected_metadata.update(best_metadata)
                selected_record["quality_evaluation_count"] = (
                    int(selected_record["quality_evaluation_count"])
                    + triple_quality_evaluation_count
                )
                selected_record["selected_objective"] = [
                    float(value) for value in best_objective
                ]
                selected_record["search_exit_quality_sha256"] = str(
                    best_quality["quality_sha256"]
                )
                selected_record["search_exit_coordinate_sha256"] = (
                    coordinate_state_sha256(component_roots)
                )
                triple_final_state = {
                    "selected_component_sha256": selected_component_sha,
                    **triple_checkpoint(
                        component_roots,
                        current_state,
                        best_quality,
                        best_objective,
                    ),
                    "triple_quality_evaluation_end_index_inclusive": (
                        evaluation_count
                    ),
                }

            triple_refinement_selection = {
                "schema": (
                    "cfdpipe.coarse_schedule_frontier_"
                    "triple_refinement_selection.v1"
                ),
                "status": "PASS",
                "gate": {
                    "static_all_components_complete_before_fine": True,
                    "single_and_pair_phases_complete_before_triple": True,
                    "static_failing_component_count": len(
                        failing_component_ids
                    ),
                    "fine_refinement_performed": bool(
                        failing_component_ids
                    ),
                    "triple_refinement_performed": bool(
                        failing_component_ids
                    ),
                    "required_triple_component_root_count": (
                        triple_required_root_count
                    ),
                    "required_triple_component_source_triangle_count": 1,
                    "selected_component_shape_valid": True,
                },
                "handoff": triple_handoff,
                "schedule": {
                    "steps_radians": pattern_steps,
                    "coordinate_passes_per_step": triple_pattern_passes,
                    "tangent_candidates_per_root_per_step": (
                        triple_root_candidate_cap
                    ),
                    "atomic_cartesian_candidates_per_step_pass": (
                        triple_atomic_candidate_cap
                    ),
                },
                "step_pass_records": triple_step_pass_records,
                "evaluation_evidence": {
                    "endpoint_maximum_quality_evaluations": (
                        endpoint_maximum_evaluations
                    ),
                    "global_conservative_quality_evaluation_cap": (
                        _TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
                    ),
                    "static_quality_evaluation_count": (
                        static_quality_evaluation_count
                    ),
                    "fine_quality_evaluation_count": (
                        fine_quality_evaluation_count
                    ),
                    "through_fine_handoff_quality_evaluation_count": (
                        fine_handoff_quality_evaluation_count
                    ),
                    "triple_quality_evaluation_count": (
                        triple_quality_evaluation_count
                    ),
                    "triple_quality_evaluation_start_index_exclusive": (
                        triple_evaluation_start
                    ),
                    "first_triple_quality_evaluation_index": (
                        first_triple_quality_evaluation_index
                    ),
                    "last_triple_quality_evaluation_index": (
                        last_triple_quality_evaluation_index
                    ),
                    "triple_quality_evaluation_end_index_inclusive": (
                        evaluation_count
                    ),
                    "final_replay_and_verification_quality_evaluation_count": None,
                    "total_quality_evaluation_count": None,
                },
                "selected_component": selected_component_evidence,
                "final_state": triple_final_state,
            }

        fine_refinement_selection = {
            "schema": (
                "cfdpipe.coarse_schedule_frontier_fine_refinement_selection.v1"
            ),
            "status": "PASS",
            "gate": {
                "static_search_started_from_fresh_a": True,
                "static_all_components_complete_before_fine": True,
                "static_component_count": len(fine_static_component_order),
                "static_failing_component_count": len(failing_component_ids),
                "maximum_refined_component_count": (
                    maximum_refined_component_count
                ),
                "required_refined_component_root_count": (
                    required_refined_root_count
                ),
                "required_refined_component_source_triangle_count": (
                    required_refined_triangle_count
                ),
                "fine_refinement_performed": bool(failing_component_ids),
            },
            "order": {
                "static_component_sha256_order": fine_static_component_order,
                "fine_component_sha256_order": fine_component_order,
                "last_static_quality_evaluation_index": (
                    static_quality_evaluation_count
                ),
                "first_fine_quality_evaluation_index": (
                    first_fine_quality_evaluation_index
                ),
                "all_static_exits_before_first_fine_evaluation": (
                    first_fine_quality_evaluation_index is None
                    or static_quality_evaluation_count
                    < first_fine_quality_evaluation_index
                ),
                "tail_steps_radians": tail_steps,
                "tail_start_coordinate_sha256": (
                    tail_start_coordinate_sha256
                ),
                "tail_start_direction_state_sha256": (
                    tail_start_direction_state_sha256
                ),
                "tail_start_direction_field_sha256": (
                    tail_start_direction_field_sha256
                ),
                "tail_start_quality_sha256": tail_start_quality_sha256,
                "tail_started_from_prefix_checkpoint": (
                    tail_started_from_prefix_checkpoint
                ),
                "first_tail_quality_evaluation_index": (
                    first_tail_quality_evaluation_index
                ),
            },
            "evaluation_evidence": {
                "endpoint_maximum_quality_evaluations": (
                    endpoint_maximum_evaluations
                ),
                "global_conservative_quality_evaluation_cap": (
                    _FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
                ),
                "static_quality_evaluation_count": (
                    static_quality_evaluation_count
                ),
                "fine_quality_evaluation_count": fine_quality_evaluation_count,
                "total_quality_evaluation_count": (
                    fine_handoff_quality_evaluation_count
                    if triple_refinement_endpoint
                    else None
                ),
            },
            "selected_component": selected_component_evidence,
            "prefix_checkpoint": prefix_checkpoint,
            "component_static_evidence": sorted(
                fine_component_static_evidence,
                key=lambda item: item["interaction_component_sha256"],
            ),
        }

    # Replay the complete absolute-coordinate state.  Pattern endpoints require
    # A -> B -> A -> B and hash both qualities and exact absolute coordinates.
    all_candidate_roots = tuple(candidate_roots)
    quality_a1, _ = evaluate(all_candidate_roots, original)
    coordinate_a1 = coordinate_state_sha256(all_candidate_roots)
    quality_b1, _ = evaluate(all_candidate_roots, selected_state)
    coordinate_b1 = coordinate_state_sha256(all_candidate_roots)
    quality_a2, _ = evaluate(all_candidate_roots, original)
    coordinate_a2 = coordinate_state_sha256(all_candidate_roots)
    state_a_reproducible = quality_a1["quality_sha256"] == quality_a2["quality_sha256"]
    coordinate_a_reproducible = coordinate_a1 == coordinate_a2
    quality_b2: dict[str, Any] | None = None
    coordinate_b2: str | None = None
    state_b_reproducible = True
    if pattern_endpoint:
        quality_b2, _ = evaluate(all_candidate_roots, selected_state)
        coordinate_b2 = coordinate_state_sha256(all_candidate_roots)
        state_b_reproducible = (
            quality_b1["quality_sha256"] == quality_b2["quality_sha256"]
            and coordinate_b1 == coordinate_b2
        )
    else:
        set_state(selected_state)
    if (
        not state_a_reproducible
        or not coordinate_a_reproducible
        or not state_b_reproducible
    ):
        set_state(original)
        raise BoundaryLayerSmokeError(
            "owner-free A-B-A-B absolute replay was not deterministic"
        )

    final_component_recheck: dict[str, Any] | None = None
    if pattern_endpoint:
        final_component_records: list[dict[str, Any]] = []
        exit_by_component = {
            str(record["interaction_component_sha256"]): record
            for record in component_records
        }
        roots_by_component = {
            str(stable["interaction_component_sha256"]): tuple(roots)
            for roots, stable in zip(
                runtime_components, search_stable_components
            )
        }
        for component_sha in sorted(component_element_sets):
            if evaluation_count >= maximum_evaluations:
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free quality cap leaves no component recheck"
                )
            element_sets = component_element_sets[component_sha]
            component_quality = _owner_free_quality_snapshot(
                gmsh,
                prism_element_tags=sorted(element_sets["prism"]),
                core_element_tags=sorted(element_sets["core"]),
                core_tetra_element_tags=sorted(element_sets["tetra"]),
                minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
                minimum_core_tetra_gamma=minimum_core_tetra_gamma,
                include_deficit_metrics=True,
            )
            evaluation_count += 1
            roots = roots_by_component[component_sha]
            penalty = sum(
                max(
                    0.0,
                    1.0
                    - _vector_dot(
                        original[root],
                        _unit_vector(selected_state[root]) or original[root],
                    ),
                )
                for root in roots
            )
            component_objective = _owner_free_quality_objective(
                component_quality,
                direction_change_penalty=penalty,
                quality_objective=objective_fields,
            )
            exit_record = exit_by_component[component_sha]
            component_coordinate_sha = coordinate_state_sha256(roots)
            if (
                component_quality["quality_sha256"]
                != exit_record["search_exit_quality_sha256"]
                or component_coordinate_sha
                != exit_record["search_exit_coordinate_sha256"]
            ):
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free final component recheck found hidden coupling"
                )
            final_component_records.append(
                {
                    "interaction_component_sha256": component_sha,
                    "selected_state_coordinate_sha256": (
                        component_coordinate_sha
                    ),
                    "affected_prism_count": len(element_sets["prism"]),
                    "affected_core_element_count": len(element_sets["core"]),
                    "affected_core_tetra_count": len(element_sets["tetra"]),
                    "quality": component_quality,
                    "final_state_objective": [
                        float(value) for value in component_objective
                    ],
                    "quality_evaluation_index": evaluation_count,
                }
            )
        recheck_unsigned = {
            "schema": "cfdpipe.owner_free_final_component_recheck.v1",
            "status": "PASS",
            "component_count": len(final_component_records),
            "quality_evaluation_count": len(final_component_records),
            "all_records_final_state": True,
            "component_records": final_component_records,
        }
        final_component_recheck = {
            **recheck_unsigned,
            "final_component_recheck_sha256": _canonical_hash(
                recheck_unsigned
            ),
        }

    if evaluation_count >= maximum_evaluations:
        set_state(original)
        raise BoundaryLayerSmokeError(
            "owner-free quality evaluation cap leaves no global verification"
        )
    final_quality = _owner_free_quality_snapshot(
        gmsh,
        prism_element_tags=all_prism_tags,
        core_element_tags=all_core_tags,
        core_tetra_element_tags=all_core_tetra_tags,
        minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
        minimum_core_tetra_gamma=minimum_core_tetra_gamma,
        include_deficit_metrics=pattern_endpoint,
    )
    evaluation_count += 1

    final_low_quality_prisms: dict[str, Any] | None = None
    if pattern_endpoint:
        if evaluation_count >= maximum_evaluations:
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free quality cap leaves no residual lineage query"
            )
        final_min_sj_values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(
                all_prism_tags, "minSJ"
            )
        ]
        evaluation_count += 1
        if len(final_min_sj_values) != len(all_prism_tags):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free final residual minSJ count is inconsistent"
            )
        prism_records_by_tag = {
            int(record["tag"]): record for record in subdivided_prisms
        }
        initial_triangle_ids = {
            str(value) for value in triangle_stable_ids.values()
        }
        low_records: list[dict[str, Any]] = []
        for tag, value in zip(all_prism_tags, final_min_sj_values):
            if math.isfinite(value) and value >= minimum_prism_scaled_jacobian:
                continue
            record = prism_records_by_tag[tag]
            lineage = prism_lineage(record)
            touched_roots = sorted(
                {
                    root
                    for node in record["nodes"]
                    for root in chain_node_to_roots.get(int(node), set())
                }
            )
            component_ids = {
                interaction_component_by_root[root]
                for root in touched_roots
            }
            if not touched_roots or len(component_ids) != 1:
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "UNEXPLAINED_FINAL_LOW_QUALITY_PRISM"
                )
            low_records.append(
                {
                    **lineage,
                    "interaction_component_sha256": next(
                        iter(component_ids)
                    ),
                    "candidate_root_coordinate_sha256": sorted(
                        _stable_root_id(root_coordinates[root])
                        for root in touched_roots
                    ),
                    "minimum_scaled_jacobian": (
                        value if math.isfinite(value) else None
                    ),
                    "quality_class": (
                        "BELOW_THRESHOLD"
                        if math.isfinite(value)
                        else "NONFINITE"
                    ),
                    "was_initial_candidate": (
                        lineage["source_triangle_sha256"]
                        in initial_triangle_ids
                    ),
                }
            )
        low_records.sort(key=lambda item: item["prism_lineage_sha256"])
        if (
            len(low_records)
            != final_quality["prism_below_threshold_element_count"]
            or len(
                {record["prism_lineage_sha256"] for record in low_records}
            )
            != len(low_records)
        ):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free final residual lineage does not match quality"
            )
        low_unsigned = {
            "schema": "cfdpipe.owner_free_low_quality_prism_inventory.v1",
            "status": "PASS",
            "minimum_scaled_jacobian_for_pass": float(
                minimum_prism_scaled_jacobian
            ),
            "final_low_quality_prism_count": len(low_records),
            "lineage_quality_query_count": 1,
            "records": low_records,
        }
        final_low_quality_prisms = {
            **low_unsigned,
            "final_low_quality_prism_inventory_sha256": _canonical_hash(
                low_unsigned
            ),
        }
    changed_roots: list[dict[str, Any]] = []
    for root in candidate_roots:
        if direction_key(selected_state[root]) == direction_key(original[root]):
            continue
        source, weight, _selected_candidate_count = selected_metadata[root]
        changed_roots.append(
            {
                "root_coordinate_sha256": _stable_root_id(root_coordinates[root]),
                "original_direction": list(original[root]),
                "selected_direction": list(selected_state[root]),
                "candidate_count": candidate_count_by_root[root],
                "selected_source": source,
                "selected_interior_weight": weight,
            }
        )
    changed_roots.sort(key=lambda item: item["root_coordinate_sha256"])
    if fine_refinement_endpoint:
        if fine_refinement_selection is None:
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free fine refinement selection evidence is missing"
            )
        active_conservative_cap = (
            _TRIPLE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
            if triple_refinement_endpoint
            else _FINE_REFINEMENT_CONSERVATIVE_QUALITY_EVALUATION_CAP
        )
        if (
            evaluation_count > active_conservative_cap
            or evaluation_count > endpoint_maximum_evaluations
        ):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free fine refinement exceeded its global quality cap"
            )
        if triple_refinement_endpoint:
            if triple_refinement_selection is None:
                set_state(original)
                raise BoundaryLayerSmokeError(
                    "owner-free triple refinement selection evidence is missing"
                )
            triple_evaluations = triple_refinement_selection[
                "evaluation_evidence"
            ]
            triple_end = int(
                triple_evaluations[
                    "triple_quality_evaluation_end_index_inclusive"
                ]
            )
            triple_evaluations[
                "final_replay_and_verification_quality_evaluation_count"
            ] = evaluation_count - triple_end
            triple_evaluations["total_quality_evaluation_count"] = (
                evaluation_count
            )
        else:
            fine_refinement_selection["evaluation_evidence"][
                "total_quality_evaluation_count"
            ] = evaluation_count
    search_status = "PASS" if final_quality["status"] == "PASS" else "EXHAUSTED"
    component_sort_key = (
        "interaction_component_sha256"
        if pattern_endpoint
        else "component_sha256"
    )
    search = {
        "schema": COMPONENT_DIRECTION_SEARCH_SCHEMA,
        "status": search_status,
        "endpoint_sha256": str(endpoint["endpoint_sha256"]),
        "sharp_owner_used": False,
        "absolute_coordinate_replay": True,
        "aba_replay_status": "PASS",
        "aba_replay": {
            "state_a_first_sha256": str(quality_a1["quality_sha256"]),
            "state_b_sha256": str(quality_b1["quality_sha256"]),
            "state_a_second_sha256": str(quality_a2["quality_sha256"]),
            "state_a_reproducible": True,
        },
        "monotonicity_assumed": False,
        "component_count": len(runtime_components),
        "candidate_root_count": len(candidate_roots),
        "candidate_direction_count": sum(candidate_count_by_root.values()),
        "quality_evaluation_count": evaluation_count,
        "coordinate_descent_passes_completed": int(
            endpoint["coordinate_descent_passes"]
        ),
        "component_records": sorted(
            component_records, key=lambda item: item[component_sort_key]
        ),
        "changed_roots": changed_roots,
    }
    if pattern_steps:
        final_minimum_cone_margin = min(
            direction_margin(root, selected_state[root])
            for root in candidate_roots
        )
        changed_final_margins = [
            direction_margin(root, selected_state[root])
            for root in candidate_roots
            if direction_key(selected_state[root])
            != direction_key(original[root])
        ]
        final_minimum_changed_direction_margin = (
            min(changed_final_margins) if changed_final_margins else None
        )
        if (
            final_minimum_cone_margin < required_cone_margin
            or (
                final_minimum_changed_direction_margin is not None
                and final_minimum_changed_direction_margin
                < required_changed_direction_margin
            )
        ):
            set_state(original)
            raise BoundaryLayerSmokeError(
                "owner-free final direction violates the configured margin"
            )
        refinement_performed = (
            fine_refinement_selection["gate"]["fine_refinement_performed"]
            if fine_refinement_endpoint
            and fine_refinement_selection is not None
            else True
        )
        search["tangent_pattern_refinement"] = {
            "performed": refinement_performed,
            "steps_radians": pattern_steps,
            "coordinate_passes_per_step": int(
                endpoint["tangent_coordinate_passes_per_step"]
            ),
            "shared_triangle_pair_moves": endpoint.get(
                "shared_triangle_pair_moves"
            )
            is True,
            "quality_evaluation_count": pattern_evaluation_count,
            "accepted_move_count": pattern_accepted_move_count,
            "refined_component_count": pattern_refined_component_count,
            "rejected_margin_candidate_count": (
                fine_rejected_margin_candidate_count
                if triple_refinement_endpoint
                else rejected_margin_candidate_count
            ),
            "minimum_required_cone_margin": (
                required_cone_margin
            ),
            "minimum_required_changed_direction_margin": (
                required_changed_direction_margin
            ),
            "entry_minimum_cone_margin": (
                original_minimum_direction_margin
            ),
            "final_minimum_cone_margin": (
                final_minimum_cone_margin
            ),
            "final_minimum_changed_direction_margin": (
                final_minimum_changed_direction_margin
            ),
        }
        search.update(
            {
                "abab_replay_status": "PASS",
                "coordinate_replay": {
                    "encoding": "python-float-hex/root-sha/layer-v1",
                    "candidate_root_count": len(candidate_roots),
                    "candidate_chain_node_count": sum(
                        len(chains[root]) for root in candidate_roots
                    ),
                    "state_a_first_coordinate_sha256": coordinate_a1,
                    "state_b_first_coordinate_sha256": coordinate_b1,
                    "state_a_second_coordinate_sha256": coordinate_a2,
                    "state_b_restored_coordinate_sha256": coordinate_b2,
                    "state_a_first_quality_sha256": str(
                        quality_a1["quality_sha256"]
                    ),
                    "state_b_first_quality_sha256": str(
                        quality_b1["quality_sha256"]
                    ),
                    "state_a_second_quality_sha256": str(
                        quality_a2["quality_sha256"]
                    ),
                    "state_b_restored_quality_sha256": str(
                        quality_b2["quality_sha256"]
                        if quality_b2 is not None
                        else quality_b1["quality_sha256"]
                    ),
                    "state_a_reproducible": coordinate_a_reproducible,
                    "state_b_reproducible": state_b_reproducible,
                    "a_b_distinct": coordinate_a1 != coordinate_b1,
                },
                "interaction_topology": interaction_topology,
                "final_component_recheck": final_component_recheck,
                "final_low_quality_prisms": final_low_quality_prisms,
            }
        )
        if fine_refinement_endpoint:
            search["fine_refinement_selection"] = fine_refinement_selection
        if triple_refinement_endpoint:
            search["triple_pattern_refinement"] = {
                "performed": bool(
                    triple_refinement_selection["gate"][
                        "triple_refinement_performed"
                    ]
                ),
                "steps_radians": pattern_steps,
                "coordinate_passes_per_step": int(
                    endpoint["triple_coordinate_passes_per_step"]
                ),
                "tangent_candidates_per_root_per_step": int(
                    endpoint["tangent_candidates_per_root_per_step"]
                ),
                "atomic_cartesian_candidates_per_step_pass": int(
                    endpoint[
                        "atomic_triple_cartesian_candidates_per_step_pass"
                    ]
                ),
                "quality_evaluation_count": triple_quality_evaluation_count,
                "accepted_move_count": triple_accepted_move_count,
                "rejected_margin_candidate_count": (
                    triple_rejected_margin_candidate_count
                ),
                "refined_component_count": (
                    1
                    if triple_refinement_selection["gate"][
                        "triple_refinement_performed"
                    ]
                    else 0
                ),
                "step_pass_records": triple_step_pass_records,
            }
            search["triple_refinement_selection"] = (
                triple_refinement_selection
            )
    return search, final_quality, {
        root: tuple(float(value) for value in selected_state[root])
        for root in candidate_roots
    }


def _rebuild_owner_free_interaction_topology(
    gmsh: Any,
    *,
    base_runtime_components: Sequence[Sequence[int]],
    base_stable_components: Sequence[Mapping[str, Any]],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
) -> dict[str, Any]:
    """Freshly reproduce the pattern audit's stable interaction topology.

    This deliberately derives every base/interaction identity from the current
    in-memory A state.  In particular, no component digest from a prior audit
    is accepted as input.  Runtime tags are used only to query this mesh; the
    resulting component identities contain only stable root/element lineage.
    """

    components = [tuple(int(root) for root in item) for item in base_runtime_components]
    stable_components = [dict(item) for item in base_stable_components]
    if not components or len(components) != len(stable_components):
        raise BoundaryLayerSmokeError(
            "physical replay base component evidence is missing or misaligned"
        )
    candidate_roots = sorted({root for component in components for root in component})
    if not candidate_roots or any(
        root not in root_coordinates or root not in chains for root in candidate_roots
    ):
        raise BoundaryLayerSmokeError(
            "physical replay candidate root lineage is incomplete"
        )

    chain_node_to_roots: dict[int, set[int]] = defaultdict(set)
    for root in candidate_roots:
        for node in chains[root]:
            chain_node_to_roots[int(node)].add(root)
    prism_tags_by_root: dict[int, set[int]] = defaultdict(set)
    for record in subdivided_prisms:
        touched = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        for root in touched:
            prism_tags_by_root[root].add(int(record["tag"]))
    core_tags_by_root: dict[int, set[int]] = defaultdict(set)
    core_tetra_tags_by_root: dict[int, set[int]] = defaultdict(set)
    for record in core_volume_records:
        touched = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        for root in touched:
            core_tags_by_root[root].add(int(record["tag"]))
            if record["type"] == "Tetrahedron 4":
                core_tetra_tags_by_root[root].add(int(record["tag"]))
    if any(
        not prism_tags_by_root[root]
        or not core_tags_by_root[root]
        or not core_tetra_tags_by_root[root]
        for root in candidate_roots
    ):
        raise BoundaryLayerSmokeError(
            "physical replay candidate root has incomplete prism/core adjacency"
        )

    all_chain_origin: dict[int, tuple[int, int]] = {}
    for root, chain in chains.items():
        for layer, node in enumerate(chain):
            previous = all_chain_origin.setdefault(int(node), (int(root), layer))
            if previous != (int(root), layer):
                raise BoundaryLayerSmokeError(
                    "physical replay layer chains share a runtime node"
                )

    def prism_lineage_sha256(record: Mapping[str, Any]) -> str:
        mapped = [all_chain_origin.get(int(node)) for node in record["nodes"]]
        if any(value is None for value in mapped):
            raise BoundaryLayerSmokeError(
                "physical replay affected Prism6 is not chain-mappable"
            )
        roots = sorted({int(value[0]) for value in mapped if value is not None})
        levels = Counter(int(value[1]) for value in mapped if value is not None)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError(
                "physical replay affected Prism6 has invalid layer lineage"
            )
        try:
            wall_fingerprint = str(
                prism_volume_fingerprints[int(record["entity"])]
            )
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "physical replay affected Prism6 lacks a stable wall fingerprint"
            ) from error
        stable = {
            "kind": "Prism6",
            "wall_surface_fingerprint": wall_fingerprint,
            "source_triangle_sha256": _stable_triangle_id(
                roots, root_coordinates
            ),
            "root_coordinate_sha256": sorted(
                _stable_root_id(root_coordinates[root]) for root in roots
            ),
            "layer_1_based": min(levels) + 1,
        }
        return _canonical_hash(stable)

    def core_lineage_sha256(record: Mapping[str, Any]) -> str:
        tokens: list[list[Any]] = []
        for raw_node in record["nodes"]:
            node = int(raw_node)
            mapped = all_chain_origin.get(node)
            if mapped is not None:
                root, layer = mapped
                tokens.append(
                    ["chain", _stable_root_id(root_coordinates[root]), int(layer)]
                )
            else:
                coordinates_value = gmsh.model.mesh.getNode(node)[0]
                tokens.append(["fixed", _stable_root_id(coordinates_value)])
        stable = {
            "kind": str(record["type"]),
            "node_lineage": sorted(tokens, key=lambda item: tuple(item)),
        }
        return _canonical_hash(stable)

    parent = {root: root for root in candidate_roots}

    def find(root: int) -> int:
        while parent[root] != root:
            parent[root] = parent[parent[root]]
            root = parent[root]
        return root

    def union(roots: Iterable[int]) -> None:
        values = sorted({int(root) for root in roots})
        if not values:
            return
        anchor = find(values[0])
        for root in values[1:]:
            other = find(root)
            if anchor != other:
                parent[max(anchor, other)] = min(anchor, other)
                anchor = find(anchor)

    base_component_by_root: dict[int, str] = {}
    for component, stable in zip(components, stable_components):
        component_sha = str(stable["component_sha256"])
        union(component)
        for root in component:
            if root in base_component_by_root:
                raise BoundaryLayerSmokeError(
                    "physical replay base components overlap"
                )
            base_component_by_root[root] = component_sha
    if set(base_component_by_root) != set(candidate_roots):
        raise BoundaryLayerSmokeError(
            "physical replay base component coverage is incomplete"
        )

    prism_records_by_tag = {
        int(record["tag"]): record for record in subdivided_prisms
    }
    core_records_by_tag = {
        int(record["tag"]): record for record in core_volume_records
    }
    if len(prism_records_by_tag) != len(subdivided_prisms) or len(
        core_records_by_tag
    ) != len(core_volume_records):
        raise BoundaryLayerSmokeError(
            "physical replay affected element tags are not unique"
        )
    affected_prism_roots: dict[int, set[int]] = {}
    affected_core_roots: dict[int, set[int]] = {}
    for tag, record in prism_records_by_tag.items():
        roots = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        if roots:
            affected_prism_roots[tag] = roots
            union(roots)
    for tag, record in core_records_by_tag.items():
        roots = {
            root
            for node in record["nodes"]
            for root in chain_node_to_roots.get(int(node), set())
        }
        if roots:
            affected_core_roots[tag] = roots
            union(roots)

    grouped: dict[int, list[int]] = defaultdict(list)
    for root in candidate_roots:
        grouped[find(root)].append(root)
    interaction_components = sorted(
        (tuple(sorted(values)) for values in grouped.values()),
        key=lambda roots: tuple(
            sorted(_stable_root_id(root_coordinates[root]) for root in roots)
        ),
    )
    interaction_stable_components: list[dict[str, Any]] = []
    component_element_sets: dict[str, dict[str, set[int]]] = {}
    interaction_component_by_root: dict[int, str] = {}
    cross_base_prism = 0
    cross_base_core = 0
    all_element_lineages: set[str] = set()
    for roots in interaction_components:
        root_set = set(roots)
        source_component_ids = sorted(
            {base_component_by_root[root] for root in roots}
        )
        affected_prism_tags = {
            tag
            for tag, touched in affected_prism_roots.items()
            if touched & root_set
        }
        affected_core_tags = {
            tag
            for tag, touched in affected_core_roots.items()
            if touched & root_set
        }
        if any(
            not touched <= root_set
            for tag, touched in affected_prism_roots.items()
            if tag in affected_prism_tags
        ) or any(
            not touched <= root_set
            for tag, touched in affected_core_roots.items()
            if tag in affected_core_tags
        ):
            raise BoundaryLayerSmokeError(
                "physical replay affected element crosses interaction components"
            )
        prism_lineages = [
            prism_lineage_sha256(prism_records_by_tag[tag])
            for tag in sorted(affected_prism_tags)
        ]
        core_lineages = [
            core_lineage_sha256(core_records_by_tag[tag])
            for tag in sorted(affected_core_tags)
        ]
        element_lineages = sorted([*prism_lineages, *core_lineages])
        if len(element_lineages) != len(set(element_lineages)):
            raise BoundaryLayerSmokeError(
                "physical replay affected element lineage is not unique"
            )
        all_element_lineages.update(element_lineages)
        stable_unsigned = {
            "root_coordinate_sha256": sorted(
                _stable_root_id(root_coordinates[root]) for root in roots
            ),
            "candidate_root_count": len(roots),
            "source_component_sha256": source_component_ids,
            "source_component_count": len(source_component_ids),
            "affected_prism_count": len(affected_prism_tags),
            "affected_core_element_count": len(affected_core_tags),
            "affected_core_tetra_count": sum(
                core_records_by_tag[tag]["type"] == "Tetrahedron 4"
                for tag in affected_core_tags
            ),
            "affected_element_lineage_sha256": _canonical_hash(
                {"element_sha256": element_lineages}
            ),
            "connected_by_affected_element_cooccurrence": True,
            "sharp_owner_used": False,
            "split_by_wall_fingerprint": False,
        }
        component_sha = _canonical_hash(stable_unsigned)
        interaction_stable_components.append(
            {
                **stable_unsigned,
                "interaction_component_sha256": component_sha,
            }
        )
        component_element_sets[component_sha] = {
            "prism": affected_prism_tags,
            "core": affected_core_tags,
            "tetra": {
                tag
                for tag in affected_core_tags
                if core_records_by_tag[tag]["type"] == "Tetrahedron 4"
            },
        }
        for root in roots:
            interaction_component_by_root[root] = component_sha

    for touched in affected_prism_roots.values():
        if len({base_component_by_root[root] for root in touched}) > 1:
            cross_base_prism += 1
    for touched in affected_core_roots.values():
        if len({base_component_by_root[root] for root in touched}) > 1:
            cross_base_core += 1
    if (
        set(interaction_component_by_root) != set(candidate_roots)
        or sum(
            len(values["prism"]) + len(values["core"])
            for values in component_element_sets.values()
        )
        != len(all_element_lineages)
    ):
        raise BoundaryLayerSmokeError(
            "physical replay interaction component coverage is incomplete"
        )
    topology_unsigned = {
        "schema": "cfdpipe.owner_free_interaction_topology.v1",
        "status": "PASS",
        "base_component_count": len(components),
        "interaction_component_count": len(interaction_components),
        "candidate_root_count": len(candidate_roots),
        "candidate_chain_node_count": sum(
            len(chains[root]) for root in candidate_roots
        ),
        "affected_prism_count": len(affected_prism_roots),
        "affected_core_element_count": len(affected_core_roots),
        "affected_core_tetra_count": sum(
            record["type"] == "Tetrahedron 4"
            for tag, record in core_records_by_tag.items()
            if tag in affected_core_roots
        ),
        "cross_base_component_prism_count": cross_base_prism,
        "cross_base_component_core_count": cross_base_core,
        "base_components_merged": len(interaction_components) < len(components),
        "components": sorted(
            interaction_stable_components,
            key=lambda item: item["interaction_component_sha256"],
        ),
    }
    interaction_topology = {
        **topology_unsigned,
        "interaction_topology_sha256": _canonical_hash(topology_unsigned),
    }
    return {
        "base_component_by_root": base_component_by_root,
        "interaction_component_by_root": interaction_component_by_root,
        "interaction_components": interaction_components,
        "component_element_sets": component_element_sets,
        "interaction_topology": interaction_topology,
    }


def _stable_root_schedule_assignments(
    *,
    candidate_roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    root_cumulative_heights: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    source_triangles: Sequence[tuple[int, int, int]],
    source_triangle_wall_fingerprints: Mapping[
        tuple[int, int, int], str
    ],
) -> dict[str, Any]:
    """Encode every selected physical root schedule without runtime tags."""

    normalized_source_triangles = sorted(
        {
            tuple(sorted(int(value) for value in triangle))
            for triangle in source_triangles
        }
    )
    if (
        not normalized_source_triangles
        or len(normalized_source_triangles) != len(source_triangles)
        or set(source_triangle_wall_fingerprints)
        != set(normalized_source_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "physical replay source-triangle schedule lineage is invalid"
        )
    records: list[dict[str, Any]] = []
    for root in candidate_roots:
        heights = [
            _finite(value, "physical replay cumulative height")
            for value in root_cumulative_heights[root]
        ]
        if (
            not heights
            or len(chains[root]) != len(heights) + 1
            or heights[0] <= 0.0
            or any(second <= first for first, second in zip(heights, heights[1:]))
        ):
            raise BoundaryLayerSmokeError(
                "physical replay root schedule is incomplete or non-monotone"
            )
        root_id = _stable_root_id(root_coordinates[root])
        incident_walls = sorted(
            {
                str(source_triangle_wall_fingerprints[triangle])
                for triangle in normalized_source_triangles
                if root in triangle
            }
        )
        if not incident_walls:
            raise BoundaryLayerSmokeError(
                "physical replay root schedule lacks incident wall lineage"
            )
        height_payload = {
            "cumulative_heights_float_hex": [value.hex() for value in heights]
        }
        records.append(
            {
                "root_coordinate_sha256": root_id,
                "incident_wall_surface_fingerprints": incident_walls,
                "cumulative_heights_sha256": _canonical_hash(height_payload),
                "selected_total_thickness_float_hex": heights[-1].hex(),
            }
        )
    records.sort(key=lambda record: record["root_coordinate_sha256"])
    if len(records) != len(candidate_roots) or len(
        {record["root_coordinate_sha256"] for record in records}
    ) != len(records):
        raise BoundaryLayerSmokeError(
            "physical replay root schedule identities are not unique"
        )
    return {
        "root_schedule_assignment_count": len(records),
        "root_schedule_assignments": records,
        "root_schedule_assignments_sha256": _canonical_hash(
            {"root_schedule_assignments": records}
        ),
    }


def _resolve_fixed_direction_replay_consensus_subgraph(
    *,
    expected_contexts: Mapping[str, Mapping[str, Any]],
    expected_candidate_root_count: int,
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    source_triangles: Sequence[Sequence[int]],
    current_bad_source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
) -> dict[str, Any]:
    """Resolve the approved minimum-growth subgraph in the full source mesh.

    Physical schedules can create additional low-quality prisms.  Those new
    failures belong to the final global quality gate; they must neither expand
    nor replace the hash-bound roots and source triangles whose directions are
    authorized for this replay.
    """

    normalized_source_triangles = sorted(
        {
            tuple(sorted(int(value) for value in triangle))
            for triangle in source_triangles
        }
    )
    if (
        not normalized_source_triangles
        or len(normalized_source_triangles) != len(source_triangles)
        or any(
            len(triangle) != 3 or len(set(triangle)) != 3
            for triangle in normalized_source_triangles
        )
        or set(source_triangle_wall_fingerprints)
        != set(normalized_source_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay source-triangle lineage is invalid"
        )
    normalized_current_bad_triangles = {
        tuple(sorted(int(value) for value in triangle))
        for triangle in current_bad_source_triangles
    }
    if (
        not normalized_current_bad_triangles
        or len(normalized_current_bad_triangles)
        != len(current_bad_source_triangles)
        or not normalized_current_bad_triangles
        <= set(normalized_source_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay current bad triangles are not a source subset"
        )

    stable_root_to_runtime: dict[str, list[int]] = defaultdict(list)
    for root in sorted(int(value) for value in chains):
        try:
            root_id = _stable_root_id(root_coordinates[root])
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "fixed-direction replay source chain lacks a root coordinate"
            ) from error
        stable_root_to_runtime[root_id].append(root)
    missing_roots = sorted(set(expected_contexts) - set(stable_root_to_runtime))
    duplicate_roots = sorted(
        root_id
        for root_id in expected_contexts
        if len(stable_root_to_runtime.get(root_id, [])) != 1
    )
    if missing_roots or duplicate_roots:
        raise BoundaryLayerSmokeError(
            "fixed-direction replay approved roots are missing or non-unique"
        )
    runtime_by_stable = {
        root_id: stable_root_to_runtime[root_id][0]
        for root_id in expected_contexts
    }

    expected_triangle_ids = {
        str(triangle_id)
        for context in expected_contexts.values()
        for triangle_id in context[
            "incident_bad_source_triangle_sha256"
        ]
    }
    triangle_by_stable: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for triangle in normalized_source_triangles:
        triangle_by_stable[
            _stable_triangle_id(triangle, root_coordinates)
        ].append(triangle)
    missing_triangles = sorted(expected_triangle_ids - set(triangle_by_stable))
    duplicate_triangles = sorted(
        triangle_id
        for triangle_id in expected_triangle_ids
        if len(triangle_by_stable.get(triangle_id, [])) != 1
    )
    if missing_triangles or duplicate_triangles:
        raise BoundaryLayerSmokeError(
            "fixed-direction replay approved source triangles are missing or non-unique"
        )
    runtime_triangle_by_stable = {
        triangle_id: triangle_by_stable[triangle_id][0]
        for triangle_id in expected_triangle_ids
    }
    approved_triangles = sorted(runtime_triangle_by_stable.values())
    approved_triangle_stable_ids = {
        triangle: triangle_id
        for triangle_id, triangle in runtime_triangle_by_stable.items()
    }
    approved_triangle_wall_fingerprints = {
        triangle: str(source_triangle_wall_fingerprints[triangle])
        for triangle in approved_triangles
    }
    try:
        base_runtime_components, base_stable_components = (
            build_owner_free_components(
                approved_triangles,
                triangle_stable_ids=approved_triangle_stable_ids,
                triangle_wall_fingerprints=(
                    approved_triangle_wall_fingerprints
                ),
                root_coordinates=root_coordinates,
            )
        )
    except CoarseComponentDirectionError as error:
        raise BoundaryLayerSmokeError(
            f"fixed-direction replay cannot rebuild approved base components: {error}"
        ) from error
    candidate_roots = sorted(
        {root for component in base_runtime_components for root in component},
        key=lambda root: _stable_root_id(root_coordinates[root]),
    )
    if len(candidate_roots) != int(expected_candidate_root_count):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay approved candidate-root count differs from consensus"
        )
    candidate_root_ids = {
        _stable_root_id(root_coordinates[root]) for root in candidate_roots
    }
    if candidate_root_ids != set(expected_contexts):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay approved roots differ from the consensus"
        )
    return {
        "current_bad_source_triangle_count": len(
            normalized_current_bad_triangles
        ),
        "candidate_roots": candidate_roots,
        "runtime_by_stable": runtime_by_stable,
        "runtime_triangle_by_stable": runtime_triangle_by_stable,
        "approved_triangle_wall_fingerprints": (
            approved_triangle_wall_fingerprints
        ),
        "base_runtime_components": base_runtime_components,
        "base_stable_components": base_stable_components,
    }


def _prepare_fixed_approved_direction_field(
    gmsh: Any,
    *,
    approval: Mapping[str, Any],
    root_coordinates: Mapping[int, Sequence[float]],
    runtime_directions: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    minimum_growth_bad_source_triangles: Sequence[Sequence[int]],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
) -> dict[str, Any]:
    """Rebuild and apply the independently approved minimum-growth field."""

    contexts = approval.get("root_contexts")
    changed_records = approval.get("changed_roots")
    if not isinstance(contexts, list) or not isinstance(changed_records, list):
        raise BoundaryLayerSmokeError(
            "fixed-direction continuation approval roots are missing"
        )
    expected_contexts = {
        str(record["root_coordinate_sha256"]): record for record in contexts
    }
    if (
        len(expected_contexts) != len(contexts)
        or len(contexts) != int(approval["candidate_root_count"])
        or len(changed_records) != int(approval["changed_root_count"])
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction continuation consensus root counts differ"
        )
    normalized_minimum_bad = sorted(
        {
            tuple(sorted(int(value) for value in triangle))
            for triangle in minimum_growth_bad_source_triangles
        }
    )
    if (
        not normalized_minimum_bad
        or len(normalized_minimum_bad)
        != len(minimum_growth_bad_source_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction continuation minimum-growth lineage is invalid"
        )
    authorization = _resolve_fixed_direction_replay_consensus_subgraph(
        expected_contexts=expected_contexts,
        expected_candidate_root_count=int(approval["candidate_root_count"]),
        root_coordinates=root_coordinates,
        chains=chains,
        source_triangles=source_triangles,
        current_bad_source_triangles=normalized_minimum_bad,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
    )
    fresh_topology = _rebuild_owner_free_interaction_topology(
        gmsh,
        base_runtime_components=authorization["base_runtime_components"],
        base_stable_components=authorization["base_stable_components"],
        root_coordinates=root_coordinates,
        chains=chains,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
    )
    expected_interaction_topology_sha256 = str(
        approval["consensus"]["interaction_topology_sha256"]
    )
    runtime_by_stable = authorization["runtime_by_stable"]
    runtime_triangle_by_stable = authorization["runtime_triangle_by_stable"]
    approved_triangle_walls = authorization[
        "approved_triangle_wall_fingerprints"
    ]
    runtime_context_records: list[dict[str, Any]] = []
    for root_id, expected in sorted(expected_contexts.items()):
        root = runtime_by_stable[root_id]
        incident_ids = sorted(
            triangle_id
            for triangle_id, triangle in runtime_triangle_by_stable.items()
            if root in triangle
        )
        walls = sorted(
            {
                str(approved_triangle_walls[triangle])
                for triangle_id, triangle in runtime_triangle_by_stable.items()
                if triangle_id in incident_ids
            }
        )
        runtime_context: dict[str, Any] = {
            "root_coordinate_sha256": root_id,
            "incident_bad_source_triangle_sha256": incident_ids,
            "incident_wall_surface_fingerprints": walls,
            "base_component_sha256": str(
                fresh_topology["base_component_by_root"][root]
            ),
            "interaction_component_sha256": str(
                fresh_topology["interaction_component_by_root"][root]
            ),
            "changed": bool(expected["changed"]),
        }
        runtime_context["root_context_sha256"] = _canonical_hash(
            runtime_context
        )
        portable_identity_keys = (
            "root_coordinate_sha256",
            "incident_bad_source_triangle_sha256",
            "incident_wall_surface_fingerprints",
            "changed",
        )
        if any(
            runtime_context[key] != expected[key]
            for key in portable_identity_keys
        ):
            raise BoundaryLayerSmokeError(
                "fixed-direction continuation root context differs"
            )
        runtime_context_records.append(dict(expected))

    roots = sorted(int(value) for value in chains)
    if set(roots) != {int(value) for value in runtime_directions}:
        raise BoundaryLayerSmokeError(
            "fixed-direction continuation runtime field is incomplete"
        )
    selected_directions: dict[int, tuple[float, float, float]] = {}
    for root in roots:
        direction = _unit_vector(runtime_directions[root])
        if direction is None:
            raise BoundaryLayerSmokeError(
                "fixed-direction continuation runtime direction is zero"
            )
        selected_directions[root] = direction
    changed_evidence: list[dict[str, Any]] = []
    changed_runtime_roots: set[int] = set()
    for record in changed_records:
        root_id = str(record["root_coordinate_sha256"])
        try:
            root = runtime_by_stable[root_id]
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "fixed-direction continuation changed root is unexplained"
            ) from error
        expected_original = tuple(
            float.fromhex(str(value))
            for value in record["original_direction_float_hex"]
        )
        actual_original = tuple(float(value) for value in runtime_directions[root])
        if max(
            abs(actual - expected)
            for actual, expected in zip(actual_original, expected_original)
        ) > _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE:
            raise BoundaryLayerSmokeError(
                "fixed-direction continuation original direction differs"
            )
        selected = tuple(
            float.fromhex(str(value))
            for value in record["selected_direction_float_hex"]
        )
        unit = _unit_vector(selected)
        if unit is None or any(
            not math.isclose(a, b, rel_tol=1.0e-12, abs_tol=1.0e-12)
            for a, b in zip(unit, selected)
        ):
            raise BoundaryLayerSmokeError(
                "fixed-direction continuation approved direction is not unit"
            )
        selected_directions[root] = selected
        changed_runtime_roots.add(root)
        changed_evidence.append(
            {
                "root_coordinate_sha256": root_id,
                "root_context_sha256": str(record["root_context_sha256"]),
                "original_direction_float_hex": list(
                    record["original_direction_float_hex"]
                ),
                "selected_direction_float_hex": list(
                    record["selected_direction_float_hex"]
                ),
            }
        )
    expected_changed_ids = {
        root_id
        for root_id, context in expected_contexts.items()
        if context["changed"] is True
    }
    if (
        {str(record["root_coordinate_sha256"]) for record in changed_records}
        != expected_changed_ids
        or len(changed_runtime_roots) != int(approval["changed_root_count"])
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction continuation changed-root coverage is incomplete"
        )
    changed_evidence.sort(key=lambda record: record["root_coordinate_sha256"])
    return {
        "directions": selected_directions,
        "direction_field_sha256": _stable_direction_field_sha256(
            roots=roots,
            root_coordinates=root_coordinates,
            directions=selected_directions,
        ),
        "changed_roots": changed_evidence,
        "changed_root_count": len(changed_runtime_roots),
        "candidate_root_count": len(expected_contexts),
        "root_context_replay": {
            "expected_count": len(expected_contexts),
            "matched_count": len(runtime_context_records),
            "missing_count": 0,
            "duplicate_count": 0,
            "unexplained_count": 0,
            "contexts": runtime_context_records,
        },
        "interaction_topology_sha256": fresh_topology[
            "interaction_topology"
        ]["interaction_topology_sha256"],
        "historical_interaction_topology_sha256": (
            expected_interaction_topology_sha256
        ),
        "portable_interaction_topology_rebuilt": True,
    }


def _run_physical_schedule_fixed_direction_replay(
    gmsh: Any,
    *,
    approval: Mapping[str, Any],
    schedule_evidence: Mapping[str, Any],
    root_coordinates: Mapping[int, Sequence[float]],
    runtime_directions: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    root_cumulative_heights: Mapping[int, Sequence[float]],
    bad_source_triangles: Sequence[Sequence[int]],
    bad_source_triangle_wall_fingerprints: Mapping[
        tuple[int, int, int], str
    ],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Replay only the consensus-approved directions on the real schedules."""

    contexts = approval.get("root_contexts")
    changed_records = approval.get("changed_roots")
    if not isinstance(contexts, list) or not isinstance(changed_records, list):
        raise BoundaryLayerSmokeError("fixed-direction replay roots are missing")
    expected_contexts = {
        str(record["root_coordinate_sha256"]): record for record in contexts
    }
    if len(expected_contexts) != len(contexts):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay consensus roots are duplicated"
        )

    normalized_bad_triangles = sorted(
        {tuple(sorted(int(value) for value in triangle)) for triangle in bad_source_triangles}
    )
    if (
        not normalized_bad_triangles
        or len(normalized_bad_triangles) != len(bad_source_triangles)
        or any(len(triangle) != 3 or len(set(triangle)) != 3 for triangle in normalized_bad_triangles)
        or set(bad_source_triangle_wall_fingerprints) != set(normalized_bad_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay A-state bad-triangle lineage is invalid"
        )

    authorization = _resolve_fixed_direction_replay_consensus_subgraph(
        expected_contexts=expected_contexts,
        expected_candidate_root_count=int(approval["candidate_root_count"]),
        root_coordinates=root_coordinates,
        chains=chains,
        source_triangles=source_triangles,
        current_bad_source_triangles=normalized_bad_triangles,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
    )
    candidate_roots = authorization["candidate_roots"]
    runtime_by_stable = authorization["runtime_by_stable"]
    runtime_triangle_by_stable = authorization[
        "runtime_triangle_by_stable"
    ]
    approved_triangle_wall_fingerprints = authorization[
        "approved_triangle_wall_fingerprints"
    ]
    base_runtime_components = authorization["base_runtime_components"]
    base_stable_components = authorization["base_stable_components"]

    fresh_topology = _rebuild_owner_free_interaction_topology(
        gmsh,
        base_runtime_components=base_runtime_components,
        base_stable_components=base_stable_components,
        root_coordinates=root_coordinates,
        chains=chains,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
    )
    interaction_topology = fresh_topology["interaction_topology"]
    try:
        expected_topology_sha256 = str(
            approval["consensus"]["interaction_topology_sha256"]
        )
    except (KeyError, TypeError) as error:
        raise BoundaryLayerSmokeError(
            "fixed-direction replay consensus topology identity is missing"
        ) from error
    if (
        interaction_topology["interaction_topology_sha256"]
        != expected_topology_sha256
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay fresh interaction topology differs from consensus"
        )
    base_component_by_root = fresh_topology["base_component_by_root"]
    interaction_component_by_root = fresh_topology[
        "interaction_component_by_root"
    ]

    runtime_context_records: list[dict[str, Any]] = []
    for root_id, expected in sorted(expected_contexts.items()):
        root = runtime_by_stable[root_id]
        incident_ids = sorted(
            triangle_id
            for triangle_id, triangle in runtime_triangle_by_stable.items()
            if root in triangle
        )
        walls = sorted(
            {
                str(approved_triangle_wall_fingerprints[triangle])
                for triangle_id, triangle in runtime_triangle_by_stable.items()
                if triangle_id in incident_ids
            }
        )
        runtime_context: dict[str, Any] = {
            "root_coordinate_sha256": root_id,
            "incident_bad_source_triangle_sha256": incident_ids,
            "incident_wall_surface_fingerprints": walls,
            "base_component_sha256": str(base_component_by_root[root]),
            "interaction_component_sha256": str(
                interaction_component_by_root[root]
            ),
            "changed": bool(expected["changed"]),
        }
        runtime_context["root_context_sha256"] = _canonical_hash(runtime_context)
        if runtime_context != expected:
            raise BoundaryLayerSmokeError(
                "fixed-direction replay root context differs from the consensus"
            )
        runtime_context_records.append(runtime_context)

    root_schedule_assignments = _stable_root_schedule_assignments(
        candidate_roots=candidate_roots,
        root_coordinates=root_coordinates,
        root_cumulative_heights=root_cumulative_heights,
        chains=chains,
        source_triangles=[
            tuple(sorted(int(value) for value in triangle))
            for triangle in source_triangles
        ],
        source_triangle_wall_fingerprints=source_triangle_wall_fingerprints,
    )

    original_state = {
        runtime_by_stable[root_id]: tuple(
            float(value) for value in runtime_directions[runtime_by_stable[root_id]]
        )
        for root_id in expected_contexts
    }
    selected_state = dict(original_state)
    changed_runtime_roots: set[int] = set()
    changed_evidence: list[dict[str, Any]] = []
    original_direction_compatibility_records: list[dict[str, Any]] = []
    for record in changed_records:
        root_id = str(record["root_coordinate_sha256"])
        root = runtime_by_stable[root_id]
        original_hex = [float(value).hex() for value in original_state[root]]
        expected_original = tuple(
            float.fromhex(str(value))
            for value in record["original_direction_float_hex"]
        )
        deltas = tuple(
            float(actual) - float(expected)
            for actual, expected in zip(
                original_state[root], expected_original
            )
        )
        maximum_absolute_difference = max(abs(value) for value in deltas)
        compatibility_record = {
            "root_coordinate_sha256": root_id,
            "runtime_original_direction_float_hex": original_hex,
            "consensus_original_direction_float_hex": list(
                record["original_direction_float_hex"]
            ),
            "maximum_absolute_difference_float_hex": (
                maximum_absolute_difference.hex()
            ),
            "l2_difference_float_hex": math.sqrt(
                math.fsum(value * value for value in deltas)
            ).hex(),
            "dot_product_float_hex": math.fsum(
                actual * expected
                for actual, expected in zip(
                    original_state[root], expected_original
                )
            ).hex(),
            "matched_exactly": (
                original_hex == record["original_direction_float_hex"]
            ),
            "within_absolute_tolerance": (
                maximum_absolute_difference
                <= _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE
            ),
        }
        if compatibility_record["within_absolute_tolerance"] is not True:
            raise BoundaryLayerSmokeError(
                "fixed-direction replay original direction exceeds the "
                "binary64 roundoff contract: "
                + json.dumps(
                    compatibility_record,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        original_direction_compatibility_records.append(
            compatibility_record
        )
        selected = tuple(
            float.fromhex(str(value))
            for value in record["selected_direction_float_hex"]
        )
        selected_norm = math.sqrt(
            math.fsum(value * value for value in selected)
        )
        if not math.isclose(
            selected_norm, 1.0, rel_tol=1.0e-12, abs_tol=1.0e-12
        ):
            raise BoundaryLayerSmokeError(
                "fixed-direction replay selected direction is not a unit vector"
            )
        selected_state[root] = selected
        changed_runtime_roots.add(root)
        changed_evidence.append(
            {
                "root_coordinate_sha256": root_id,
                "root_context_sha256": str(record["root_context_sha256"]),
                "original_direction_float_hex": list(
                    record["original_direction_float_hex"]
                ),
                "selected_direction_float_hex": list(
                    record["selected_direction_float_hex"]
                ),
                "application": "ABSOLUTE_ORIGIN_PLUS_DIRECTION_TIMES_REAL_SCHEDULE",
            }
        )
    original_direction_compatibility_records.sort(
        key=lambda record: record["root_coordinate_sha256"]
    )
    compatibility_unsigned = {
        "schema": "cfdpipe.coarse_direction_roundoff_compatibility.v1",
        "status": "PASS",
        "absolute_tolerance_float_hex": (
            _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE.hex()
        ),
        "state_a_uses_fresh_runtime_direction": True,
        "selected_state_uses_consensus_direction": True,
        "changed_root_count": len(original_direction_compatibility_records),
        "exact_match_root_count": sum(
            record["matched_exactly"] is True
            for record in original_direction_compatibility_records
        ),
        "roundoff_match_root_count": sum(
            record["matched_exactly"] is False
            for record in original_direction_compatibility_records
        ),
        "records": original_direction_compatibility_records,
        "records_sha256": _canonical_hash(
            {"records": original_direction_compatibility_records}
        ),
    }
    original_direction_compatibility = {
        **compatibility_unsigned,
        "compatibility_sha256": _canonical_hash(compatibility_unsigned),
    }
    expected_changed_ids = {
        root_id
        for root_id, context in expected_contexts.items()
        if context["changed"] is True
    }
    if (
        {str(record["root_coordinate_sha256"]) for record in changed_records}
        != expected_changed_ids
        or len(changed_runtime_roots) != int(approval["changed_root_count"])
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay changed-root coverage is incomplete"
        )

    changed_chain_nodes = {
        int(node)
        for root in changed_runtime_roots
        for node in chains[root][1:]
    }
    before_all_coordinates = _nodes_from_gmsh(gmsh)
    non_target_nodes = sorted(set(before_all_coordinates) - changed_chain_nodes)

    def exact_coordinate_multiset(
        node_tags: Sequence[int], coordinates: Mapping[int, Sequence[float]]
    ) -> str:
        records = sorted(
            tuple(float(value).hex() for value in coordinates[int(node)])
            for node in node_tags
        )
        return _canonical_hash(
            {"encoding": "python-float-hex-coordinate-multiset-v1", "records": records}
        )

    non_target_before_sha = exact_coordinate_multiset(
        non_target_nodes, before_all_coordinates
    )
    unchanged_roots = sorted(set(candidate_roots) - changed_runtime_roots)
    unchanged_before = {
        _stable_root_id(root_coordinates[root]): [
            tuple(
                float(value).hex()
                for value in before_all_coordinates[int(node)]
            )
            for node in chains[root]
        ]
        for root in unchanged_roots
    }

    def set_changed_state(state: Mapping[int, Sequence[float]]) -> None:
        for root in sorted(changed_runtime_roots):
            origin = root_coordinates[root]
            direction = state[root]
            heights = root_cumulative_heights[root]
            if len(chains[root]) != len(heights) + 1:
                raise BoundaryLayerSmokeError(
                    "fixed-direction replay chain/schedule length differs"
                )
            for layer, node in enumerate(chains[root][1:]):
                target = _vector_add(
                    origin, _vector_scale(direction, float(heights[layer]))
                )
                existing = gmsh.model.mesh.getNode(int(node))
                gmsh.model.mesh.setNode(int(node), list(target), list(existing[1]))

    def coordinate_state_sha256() -> str:
        records: list[dict[str, Any]] = []
        for root in candidate_roots:
            root_id = _stable_root_id(root_coordinates[root])
            for layer, node in enumerate(chains[root]):
                values = [
                    float(value)
                    for value in gmsh.model.mesh.getNode(int(node))[0]
                ]
                if len(values) != 3 or any(
                    not math.isfinite(value) for value in values
                ):
                    raise BoundaryLayerSmokeError(
                        "physical replay coordinate is non-finite"
                    )
                records.append(
                    {
                        "root_coordinate_sha256": root_id,
                        "layer": layer,
                        "coordinates_float_hex": [
                            value.hex() for value in values
                        ],
                    }
                )
        return _canonical_hash(
            {
                "encoding": "python-float-hex/root-sha/physical-layer-v1",
                "records": records,
            }
        )

    prism_tags = [int(record["tag"]) for record in subdivided_prisms]
    core_tags = [int(record["tag"]) for record in core_volume_records]
    core_tetra_tags = [
        int(record["tag"])
        for record in core_volume_records
        if record["type"] == "Tetrahedron 4"
    ]

    def quality_state() -> dict[str, Any]:
        return _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=prism_tags,
            core_element_tags=core_tags,
            core_tetra_element_tags=core_tetra_tags,
            minimum_prism_scaled_jacobian=(
                minimum_prism_scaled_jacobian
            ),
            minimum_core_tetra_gamma=minimum_core_tetra_gamma,
            include_deficit_metrics=True,
        )

    set_changed_state(original_state)
    coordinate_a1 = coordinate_state_sha256()
    quality_a1 = quality_state()
    set_changed_state(selected_state)
    coordinate_b1 = coordinate_state_sha256()
    quality_b1 = quality_state()
    set_changed_state(original_state)
    coordinate_a2 = coordinate_state_sha256()
    quality_a2 = quality_state()
    set_changed_state(selected_state)
    coordinate_b2 = coordinate_state_sha256()
    quality_b2 = quality_state()
    if (
        coordinate_a1 != coordinate_a2
        or coordinate_b1 != coordinate_b2
        or coordinate_a1 == coordinate_b1
        or quality_a1["quality_sha256"] != quality_a2["quality_sha256"]
        or quality_b1["quality_sha256"] != quality_b2["quality_sha256"]
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule fixed-direction A-B-A-B replay is not deterministic"
        )

    after_all_coordinates = _nodes_from_gmsh(gmsh)
    non_target_after_sha = exact_coordinate_multiset(
        non_target_nodes, after_all_coordinates
    )
    non_target_changed_count = sum(
        before_all_coordinates[node] != after_all_coordinates[node]
        for node in non_target_nodes
    )
    unchanged_chain_changed_count = 0
    for root in unchanged_roots:
        root_id = _stable_root_id(root_coordinates[root])
        after = [
            tuple(
                float(value).hex()
                for value in after_all_coordinates[int(node)]
            )
            for node in chains[root]
        ]
        unchanged_chain_changed_count += after != unchanged_before[root_id]
    base_root_moved_count = sum(
        tuple(after_all_coordinates[root]) != tuple(root_coordinates[root])
        for root in candidate_roots
    )
    if (
        non_target_changed_count != 0
        or non_target_before_sha != non_target_after_sha
        or unchanged_chain_changed_count != 0
        or base_root_moved_count != 0
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction replay changed a non-authorized coordinate"
        )

    interaction_recheck: list[dict[str, Any]] = []
    context_groups: dict[str, set[int]] = defaultdict(set)
    for context in contexts:
        context_groups[str(context["interaction_component_sha256"])].add(
            runtime_by_stable[str(context["root_coordinate_sha256"])]
        )
    component_element_sets = fresh_topology["component_element_sets"]
    if set(context_groups) != set(component_element_sets):
        raise BoundaryLayerSmokeError(
            "physical replay interaction component evidence is incomplete"
        )
    for component_id, component_roots in sorted(context_groups.items()):
        affected_prisms = sorted(component_element_sets[component_id]["prism"])
        affected_core = sorted(component_element_sets[component_id]["core"])
        affected_tetra = sorted(component_element_sets[component_id]["tetra"])
        component_quality = _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=affected_prisms,
            core_element_tags=affected_core,
            core_tetra_element_tags=affected_tetra,
            minimum_prism_scaled_jacobian=(
                minimum_prism_scaled_jacobian
            ),
            minimum_core_tetra_gamma=minimum_core_tetra_gamma,
            include_deficit_metrics=True,
        )
        interaction_recheck.append(
            {
                "interaction_component_sha256": component_id,
                "candidate_root_count": len(component_roots),
                "affected_prism_count": len(affected_prisms),
                "affected_core_element_count": len(affected_core),
                "affected_core_tetra_count": len(affected_tetra),
                "quality": component_quality,
            }
        )

    min_sj_values = [
        float(value)
        for value in gmsh.model.mesh.getElementQualities(prism_tags, "minSJ")
    ]
    prism_records_by_tag = {
        int(record["tag"]): record for record in subdivided_prisms
    }
    final_low_quality: list[dict[str, Any]] = []
    for prism, value in zip(prism_tags, min_sj_values):
        if math.isfinite(value) and value >= minimum_prism_scaled_jacobian:
            continue
        record = prism_records_by_tag[prism]
        mapped = [chain_origin[int(node)] for node in record["nodes"]]
        roots = sorted({int(item[0]) for item in mapped})
        levels = sorted({int(item[1]) for item in mapped})
        triangle = tuple(roots)
        final_low_quality.append(
            {
                "source_triangle_sha256": _stable_triangle_id(
                    triangle, root_coordinates
                ),
                "wall_surface_fingerprint": str(
                    prism_volume_fingerprints[int(record["entity"])]
                ),
                "layer": min(levels) + 1,
                "minimum_scaled_jacobian_float_hex": value.hex(),
            }
        )
    final_low_quality.sort(
        key=lambda record: (
            record["source_triangle_sha256"],
            record["layer"],
            record["minimum_scaled_jacobian_float_hex"],
        )
    )
    final_status = "PASS" if quality_b2["status"] == "PASS" else "INCOMPLETE"
    unsigned_discovery: dict[str, Any] = {
        "schema": "cfdpipe.coarse_physical_schedule_fixed_direction_discovery.v1",
        "status": final_status,
        "profile_complete": final_status == "PASS",
        "audit_only": True,
        "audit_variant": "physical_local_schedule_fixed_direction_replay",
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "runtime_mesh_tags_hardcoded": False,
        "source_bindings": {
            "direction_replay_approval_sha256": str(
                approval["approval_sha256"]
            ),
            "source_audit_manifest_sha256": [
                str(record["sha256"])
                for record in approval["source_audit_manifests"]
            ],
            "local_schedule_binding_sha256": str(
                schedule_evidence["binding_sha256"]
            ),
        },
        "fixed_direction_contract": {
            "candidate_root_count": len(candidate_roots),
            "changed_root_count": len(changed_runtime_roots),
            "unchanged_root_count": len(unchanged_roots),
            "direction_search_performed": False,
            "absolute_coordinate_replay": True,
            "original_direction_compatibility": (
                original_direction_compatibility
            ),
            "changed_roots": changed_evidence,
        },
        "root_context_replay": {
            "expected_count": len(expected_contexts),
            "matched_count": len(runtime_context_records),
            "missing_count": 0,
            "duplicate_count": 0,
            "unexplained_count": 0,
            "contexts": runtime_context_records,
        },
        "schedule_application": {
            "minimum_growth_schedule_used": False,
            "surface_count": int(schedule_evidence["surface_count"]),
            "layer_count": len(next(iter(root_cumulative_heights.values()))),
            "baseline_surface_schedules_sha256": str(
                schedule_evidence["baseline_surface_schedules_sha256"]
            ),
            "minimum_surface_total_thickness_m": float(
                schedule_evidence["minimum_surface_total_thickness_m"]
            ),
            "maximum_surface_total_thickness_m": float(
                schedule_evidence["maximum_surface_total_thickness_m"]
            ),
            **root_schedule_assignments,
        },
        "coordinate_replay": {
            "encoding": "python-float-hex/root-sha/physical-layer-v1",
            "state_a_first_coordinate_sha256": coordinate_a1,
            "state_b_first_coordinate_sha256": coordinate_b1,
            "state_a_second_coordinate_sha256": coordinate_a2,
            "state_b_second_coordinate_sha256": coordinate_b2,
            "state_a_first_quality_sha256": quality_a1["quality_sha256"],
            "state_b_first_quality_sha256": quality_b1["quality_sha256"],
            "state_a_second_quality_sha256": quality_a2["quality_sha256"],
            "state_b_second_quality_sha256": quality_b2["quality_sha256"],
            "state_a_reproducible": True,
            "state_b_reproducible": True,
            "a_b_distinct": True,
            "base_root_moved_count": base_root_moved_count,
            "unchanged_chain_changed_count": unchanged_chain_changed_count,
            "non_target_node_changed_count": non_target_changed_count,
            "non_target_coordinate_sha256": non_target_after_sha,
        },
        "interaction_recheck": interaction_recheck,
        "final_quality": quality_b2,
        "final_low_quality_prisms": {
            "count": len(final_low_quality),
            "records": final_low_quality,
            "records_sha256": _canonical_hash({"records": final_low_quality}),
        },
        "observed_counts": {
            "projected_3d_element_count": len(prism_tags) + len(core_tags),
            "prism_element_count": len(prism_tags),
            "core_element_count": len(core_tags),
            "core_tetra_count": len(core_tetra_tags),
            "candidate_root_count": len(candidate_roots),
            "changed_root_count": len(changed_runtime_roots),
            "unchanged_root_count": len(unchanged_roots),
            "applied_direction_count": len(changed_runtime_roots),
            "searched_direction_count": 0,
        },
        "incomplete_reason": (
            None
            if final_status == "PASS"
            else "PHYSICAL_LOCAL_SCHEDULE_FIXED_DIRECTION_REPLAY_QUALITY_FAILED"
        ),
    }
    return {
        **unsigned_discovery,
        "discovery_sha256": _canonical_hash(unsigned_discovery),
    }


def _homotopy_coordinate_state_sha256(
    gmsh: Any,
    *,
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
) -> str:
    """Hash all layer-chain coordinates without retaining a second full mesh."""

    all_coordinates = _nodes_from_gmsh(gmsh)
    digest = hashlib.sha256()
    digest.update(b"cfdpipe-homotopy-root-sha-layer-float-hex-v1\n")
    ordered_roots = sorted(
        (int(root) for root in roots),
        key=lambda root: _stable_root_id(root_coordinates[root]),
    )
    for root in ordered_roots:
        root_id = _stable_root_id(root_coordinates[root])
        for layer, node in enumerate(chains[root]):
            try:
                values = [float(value) for value in all_coordinates[int(node)]]
            except KeyError as error:
                raise BoundaryLayerSmokeError(
                    "physical-schedule homotopy chain node is missing"
                ) from error
            if len(values) != 3 or any(not math.isfinite(value) for value in values):
                raise BoundaryLayerSmokeError(
                    "physical-schedule homotopy coordinate is non-finite"
                )
            digest.update(
                (
                    root_id
                    + "|"
                    + str(layer)
                    + "|"
                    + "|".join(value.hex() for value in values)
                    + "\n"
                ).encode("ascii")
            )
    return digest.hexdigest()


def _homotopy_root_schedule_sha256(
    *,
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    root_schedules: Mapping[int, Sequence[float]],
) -> str:
    """Hash root schedules by stable coordinate identity, never runtime tag."""

    records = [
        {
            "root_coordinate_sha256": _stable_root_id(root_coordinates[int(root)]),
            "cumulative_heights_float_hex": [
                float(value).hex() for value in root_schedules[int(root)]
            ],
        }
        for root in roots
    ]
    records.sort(key=lambda record: record["root_coordinate_sha256"])
    return _canonical_hash({"records": records})


def _homotopy_low_quality_aggregate(
    gmsh: Any,
    *,
    prism_element_tags: Sequence[int],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    chain_origin: Mapping[int, tuple[int, int]],
    root_coordinates: Mapping[int, Sequence[float]],
    prism_volume_fingerprints: Mapping[int, str],
    minimum_prism_scaled_jacobian: float,
) -> dict[str, Any]:
    """Return stable wall/layer aggregates for one homotopy quality state."""

    _runtime_triangles, _records, aggregate = (
        _stable_low_quality_prism_lineage(
            gmsh,
            prism_element_tags=prism_element_tags,
            subdivided_prisms=subdivided_prisms,
            chain_origin=chain_origin,
            root_coordinates=root_coordinates,
            prism_volume_fingerprints=prism_volume_fingerprints,
            minimum_prism_scaled_jacobian=(
                minimum_prism_scaled_jacobian
            ),
        )
    )
    return aggregate


def _stable_low_quality_prism_lineage(
    gmsh: Any,
    *,
    prism_element_tags: Sequence[int],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    chain_origin: Mapping[int, tuple[int, int]],
    root_coordinates: Mapping[int, Sequence[float]],
    prism_volume_fingerprints: Mapping[int, str],
    minimum_prism_scaled_jacobian: float,
    prism_quality_values: Mapping[int, float] | None = None,
) -> tuple[
    list[tuple[int, int, int]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Collect stable low-Prism lineage and its homotopy-compatible aggregate.

    Runtime root tags are returned separately for the in-memory continuation
    search.  Persisted records contain only stable triangle/wall/layer
    identities and therefore hash identically to the earlier homotopy scan.
    """

    tags = [int(value) for value in prism_element_tags]
    threshold = _finite(
        minimum_prism_scaled_jacobian,
        "continuation minimum Prism6 scaled Jacobian",
    )
    if not tags or len(tags) != len(set(tags)) or threshold <= 0.0:
        raise BoundaryLayerSmokeError(
            "physical-schedule continuation Prism6 inventory is invalid"
        )
    if prism_quality_values is None:
        values = [
            float(value)
            for value in gmsh.model.mesh.getElementQualities(tags, "minSJ")
        ]
    else:
        normalized_values = {
            int(tag): float(value)
            for tag, value in prism_quality_values.items()
        }
        if set(normalized_values) != set(tags):
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation supplied Prism6 quality "
                "coverage is incomplete"
            )
        values = [normalized_values[tag] for tag in tags]
    if len(values) != len(tags):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy Prism6 quality query is incomplete"
        )
    records_by_tag = {
        int(record["tag"]): record for record in subdivided_prisms
    }
    if set(records_by_tag) != set(tags):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy Prism6 lineage is incomplete"
        )
    records: list[dict[str, Any]] = []
    wall_layer_triangles: dict[tuple[str, int], set[str]] = defaultdict(set)
    wall_triangles: dict[str, set[str]] = defaultdict(set)
    layer_triangles: dict[int, set[str]] = defaultdict(set)
    wall_layer_counts: Counter[tuple[str, int]] = Counter()
    wall_counts: Counter[str] = Counter()
    layer_counts: Counter[int] = Counter()
    for tag, value in zip(tags, values):
        if math.isfinite(value) and value >= threshold:
            continue
        record = records_by_tag[tag]
        try:
            mapped = [chain_origin[int(node)] for node in record["nodes"]]
            wall = str(prism_volume_fingerprints[int(record["entity"])]).casefold()
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy low-quality lineage is incomplete"
            ) from error
        roots = sorted({int(item[0]) for item in mapped})
        levels = Counter(int(item[1]) for item in mapped)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy Prism6 source topology is invalid"
            )
        layer = min(levels) + 1
        triangle = tuple(roots)
        triangle_id = _stable_triangle_id(triangle, root_coordinates)
        records.append(
            {
                "source_triangle_sha256": triangle_id,
                "wall_surface_fingerprint": wall,
                "layer": layer,
                "minimum_scaled_jacobian_float_hex": value.hex(),
            }
        )
        key = (wall, layer)
        wall_layer_counts[key] += 1
        wall_counts[wall] += 1
        layer_counts[layer] += 1
        wall_layer_triangles[key].add(triangle_id)
        wall_triangles[wall].add(triangle_id)
        layer_triangles[layer].add(triangle_id)
    records.sort(
        key=lambda record: (
            record["wall_surface_fingerprint"],
            record["layer"],
            record["source_triangle_sha256"],
            record["minimum_scaled_jacobian_float_hex"],
        )
    )
    wall_layer = [
        {
            "wall_surface_fingerprint": wall,
            "layer": layer,
            "count": wall_layer_counts[(wall, layer)],
            "unique_source_triangle_count": len(wall_layer_triangles[(wall, layer)]),
        }
        for wall, layer in sorted(wall_layer_counts)
    ]
    by_wall = [
        {
            "wall_surface_fingerprint": wall,
            "count": wall_counts[wall],
            "unique_source_triangle_count": len(wall_triangles[wall]),
        }
        for wall in sorted(wall_counts)
    ]
    by_layer = [
        {
            "layer": layer,
            "count": layer_counts[layer],
            "unique_source_triangle_count": len(layer_triangles[layer]),
        }
        for layer in sorted(layer_counts)
    ]
    unsigned = {
        "count": len(records),
        "unique_source_triangle_count": len(
            {record["source_triangle_sha256"] for record in records}
        ),
        "wall_count": len(by_wall),
        "layer_count": len(by_layer),
        "by_wall_and_layer": wall_layer,
        "by_wall": by_wall,
        "by_layer": by_layer,
        "records_sha256": _canonical_hash({"records": records}),
    }
    runtime_triangles = sorted(
        {
            tuple(
                sorted(
                    {
                        int(item[0])
                    for item in (
                        chain_origin[int(node)] for node in records_by_tag[tag]["nodes"]
                    )
                    }
                )
            )
            for tag, value in zip(tags, values)
            if not (math.isfinite(value) and value >= threshold)
        }
    )
    if (
        any(len(triangle) != 3 or len(set(triangle)) != 3 for triangle in runtime_triangles)
        or len(runtime_triangles)
        != unsigned["unique_source_triangle_count"]
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule continuation bad-triangle lineage is inconsistent"
        )
    return (
        runtime_triangles,
        records,
        {**unsigned, "aggregate_sha256": _canonical_hash(unsigned)},
    )


def _stable_direction_field_sha256(
    *,
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    directions: Mapping[int, Sequence[float]],
) -> str:
    """Hash one complete unit direction field by stable root identity."""

    normalized_roots = sorted({int(value) for value in roots})
    if (
        not normalized_roots
        or len(normalized_roots) != len(roots)
        or set(normalized_roots) != {int(value) for value in directions}
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule continuation direction coverage is incomplete"
        )
    records: list[dict[str, Any]] = []
    for root in normalized_roots:
        try:
            direction = _unit_vector(directions[root])
            root_id = _stable_root_id(root_coordinates[root])
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation direction root is missing"
            ) from error
        if direction is None:
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation direction is zero"
            )
        records.append(
            {
                "root_coordinate_sha256": root_id,
                "direction_float_hex": [float(value).hex() for value in direction],
            }
        )
    records.sort(key=lambda record: record["root_coordinate_sha256"])
    if len({record["root_coordinate_sha256"] for record in records}) != len(records):
        raise BoundaryLayerSmokeError(
            "physical-schedule continuation stable root identity is ambiguous"
        )
    return _canonical_hash({"records": records})


def _frontier_component_precheck(
    components: Sequence[Sequence[int]],
    *,
    caps: Mapping[str, Any],
) -> dict[str, Any]:
    """Enforce the small first-frontier scope before any direction mutation."""

    normalized = [tuple(sorted({int(root) for root in item})) for item in components]
    candidate_roots = sorted({root for item in normalized for root in item})
    try:
        maximum_components = _positive_int(
            caps["maximum_component_count"],
            "frontier maximum component count",
        )
        maximum_roots = _positive_int(
            caps["maximum_candidate_root_count"],
            "frontier maximum candidate root count",
        )
    except (KeyError, TypeError) as error:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier caps are incomplete"
        ) from error
    if (
        not normalized
        or any(not item for item in normalized)
        or len(candidate_roots) != sum(len(item) for item in normalized)
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier components overlap or are empty"
        )
    evidence = {
        "component_count": len(normalized),
        "candidate_root_count": len(candidate_roots),
        "maximum_component_count": maximum_components,
        "maximum_candidate_root_count": maximum_roots,
        "within_caps": (
            len(normalized) <= maximum_components
            and len(candidate_roots) <= maximum_roots
        ),
    }
    if not evidence["within_caps"]:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier exceeds its bounded component scope: "
            f"component_count={evidence['component_count']} "
            f"maximum_component_count={maximum_components} "
            f"candidate_root_count={evidence['candidate_root_count']} "
            f"maximum_candidate_root_count={maximum_roots}"
        )
    return {**evidence, "candidate_roots": candidate_roots}


def _apply_absolute_fraction_chain_state(
    gmsh: Any,
    *,
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    root_schedules: Mapping[int, Sequence[float]],
    directions: Mapping[int, Sequence[float]],
    parametric_coordinates_by_node: Mapping[int, Sequence[float]],
) -> dict[str, Any]:
    """Rebuild every layer node from its immutable root origin."""

    normalized_roots = sorted({int(value) for value in roots})
    if (
        not normalized_roots
        or len(normalized_roots) != len(roots)
        or set(normalized_roots) != {int(value) for value in chains}
        or set(normalized_roots) != {int(value) for value in root_schedules}
        or set(normalized_roots) != {int(value) for value in directions}
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule continuation absolute state is incomplete"
        )
    first_height_hex: str | None = None
    updated_nodes: set[int] = set()
    for root in normalized_roots:
        try:
            origin = tuple(float(value) for value in root_coordinates[root])
            chain = [int(value) for value in chains[root]]
            schedule = [float(value) for value in root_schedules[root]]
            direction = _unit_vector(directions[root])
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation absolute root state is missing"
            ) from error
        if (
            len(origin) != 3
            or any(not math.isfinite(value) for value in origin)
            or direction is None
            or not schedule
            or len(chain) != len(schedule) + 1
            or chain[0] != root
            or any(not math.isfinite(value) or value <= 0.0 for value in schedule)
            or any(right <= left for left, right in zip(schedule, schedule[1:]))
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation chain/schedule invariant failed"
            )
        current_first_hex = schedule[0].hex()
        if first_height_hex is None:
            first_height_hex = current_first_hex
        elif current_first_hex != first_height_hex:
            raise BoundaryLayerSmokeError(
                "physical-schedule continuation changed the exact first height"
            )
        for layer, node in enumerate(chain[1:]):
            if node in updated_nodes:
                raise BoundaryLayerSmokeError(
                    "physical-schedule continuation layer chains share a node"
                )
            try:
                parametric = list(parametric_coordinates_by_node[node])
            except KeyError as error:
                raise BoundaryLayerSmokeError(
                    "physical-schedule continuation parametric coordinate is missing"
                ) from error
            target = _vector_add(
                origin,
                _vector_scale(direction, schedule[layer]),
            )
            gmsh.model.mesh.setNode(node, list(target), parametric)
            updated_nodes.add(node)
    coordinate_sha256 = _homotopy_coordinate_state_sha256(
        gmsh,
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        chains=chains,
    )
    return {
        "root_schedule_sha256": _homotopy_root_schedule_sha256(
            roots=normalized_roots,
            root_coordinates=root_coordinates,
            root_schedules=root_schedules,
        ),
        "direction_field_sha256": _stable_direction_field_sha256(
            roots=normalized_roots,
            root_coordinates=root_coordinates,
            directions=directions,
        ),
        "coordinate_sha256": coordinate_sha256,
        "first_layer_height_float_hex": first_height_hex,
        "root_count": len(normalized_roots),
        "updated_chain_node_count": len(updated_nodes),
        "absolute_origin_rebuild": True,
    }


def _run_physical_schedule_homotopy_scan(
    gmsh: Any,
    *,
    endpoint: Mapping[str, Any],
    authoritative_surface_schedules: Mapping[str, Sequence[float]],
    expected_surface_fingerprints: Sequence[str],
    source_prisms: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
    root_coordinates: Mapping[int, Sequence[float]],
    selected_directions: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
    expected_minimum_growth_quality_sha256: str,
) -> dict[str, Any]:
    """Replay every configured schedule fraction forwards and backwards."""

    fractions = [float(value) for value in endpoint.get("fractions", [])]
    if (
        not fractions
        or fractions != sorted(set(fractions))
        or fractions[0] != 0.0
        or fractions[-1] != 1.0
        or any(not math.isfinite(value) or value < 0.0 or value > 1.0 for value in fractions)
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy fractions are incomplete or unordered"
        )
    roots = sorted(int(value) for value in chains)
    if (
        not roots
        or not set(roots) <= {int(value) for value in root_coordinates}
        or set(roots) != {int(value) for value in selected_directions}
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy root coverage is incomplete"
        )
    normalized_directions: dict[int, tuple[float, float, float]] = {}
    for root in roots:
        direction = _unit_vector(selected_directions[root])
        if direction is None:
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy direction is zero"
            )
        normalized_directions[root] = direction
    first_heights = {
        float(values[0]).hex() for values in authoritative_surface_schedules.values()
    }
    if len(first_heights) != 1:
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy has no exact common first height"
        )
    first_height_hex = next(iter(first_heights))
    prism_tags = [int(record["tag"]) for record in subdivided_prisms]
    core_tags = [int(record["tag"]) for record in core_volume_records]
    core_tetra_tags = [
        int(record["tag"])
        for record in core_volume_records
        if record["type"] == "Tetrahedron 4"
    ]
    updated_nodes = {
        int(node) for root in roots for node in chains[root][1:]
    }
    expected_updated_node_count = sum(len(chains[root]) - 1 for root in roots)
    if (
        any(len(chains[root]) < 2 for root in roots)
        or len(updated_nodes) != expected_updated_node_count
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy layer chain is incomplete"
        )
    parametric_coordinates_by_node: dict[int, list[float]] = {}
    for node in sorted(updated_nodes):
        existing = gmsh.model.mesh.getNode(node)
        if not isinstance(existing, Sequence) or len(existing) < 2:
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy node metadata is incomplete"
            )
        values = [float(value) for value in existing[0]]
        parameters = [float(value) for value in existing[1]]
        if len(values) != 3 or any(
            not math.isfinite(value) for value in (*values, *parameters)
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy node metadata is non-finite"
            )
        parametric_coordinates_by_node[node] = parameters

    def apply_fraction(fraction: float) -> dict[str, Any]:
        try:
            surface_schedules, interpolation_evidence = (
                interpolate_physical_schedule_homotopy(
                    authoritative_surface_schedules,
                    endpoint,
                    fraction,
                    expected_surface_fingerprints=expected_surface_fingerprints,
                )
            )
        except CoarseDirectionReplayError as error:
            raise BoundaryLayerSmokeError(
                f"physical-schedule homotopy interpolation failed: {error}"
            ) from error
        if {
            float(values[0]).hex() for values in surface_schedules.values()
        } != {first_height_hex}:
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy changed the exact first height"
            )
        root_schedules, _root_schedule_evidence = _assign_root_local_schedules(
            source_prisms,
            prism_volume_fingerprints=prism_volume_fingerprints,
            surface_schedules=surface_schedules,
            root_coordinates=root_coordinates,
        )
        if set(root_schedules) != set(roots) or any(
            float(values[0]).hex() != first_height_hex
            for values in root_schedules.values()
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy root schedule coverage changed"
            )
        for root in roots:
            origin = root_coordinates[root]
            direction = normalized_directions[root]
            heights = root_schedules[root]
            if len(chains[root]) != len(heights) + 1:
                raise BoundaryLayerSmokeError(
                    "physical-schedule homotopy chain/schedule length differs"
                )
            for layer, node in enumerate(chains[root][1:]):
                target = _vector_add(
                    origin, _vector_scale(direction, float(heights[layer]))
                )
                gmsh.model.mesh.setNode(
                    int(node),
                    list(target),
                    parametric_coordinates_by_node[int(node)],
                )
        root_schedule_sha256 = _homotopy_root_schedule_sha256(
            roots=roots,
            root_coordinates=root_coordinates,
            root_schedules=root_schedules,
        )
        coordinate_sha256 = _homotopy_coordinate_state_sha256(
            gmsh,
            roots=roots,
            root_coordinates=root_coordinates,
            chains=chains,
        )
        quality = _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=prism_tags,
            core_element_tags=core_tags,
            core_tetra_element_tags=core_tetra_tags,
            minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
            minimum_core_tetra_gamma=minimum_core_tetra_gamma,
            include_deficit_metrics=True,
        )
        low_quality_aggregate = _homotopy_low_quality_aggregate(
            gmsh,
            prism_element_tags=prism_tags,
            subdivided_prisms=subdivided_prisms,
            chain_origin=chain_origin,
            root_coordinates=root_coordinates,
            prism_volume_fingerprints=prism_volume_fingerprints,
            minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
        )
        if low_quality_aggregate["count"] != quality[
            "prism_below_threshold_element_count"
        ]:
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy low-quality aggregate/count differs"
            )
        return {
            "fraction": fraction,
            "fraction_float_hex": fraction.hex(),
            "interpolation_evidence": interpolation_evidence,
            "root_schedule_sha256": root_schedule_sha256,
            "coordinate_sha256": coordinate_sha256,
            "quality": quality,
            "low_quality_aggregate": low_quality_aggregate,
        }

    ascending_scan = [apply_fraction(fraction) for fraction in fractions]
    descending_replay = [apply_fraction(fraction) for fraction in reversed(fractions)]
    ascending_by_fraction = {
        record["fraction_float_hex"]: record for record in ascending_scan
    }
    reproducibility_fields = (
        "interpolation_evidence",
        "root_schedule_sha256",
        "coordinate_sha256",
        "quality",
        "low_quality_aggregate",
    )
    for replay in descending_replay:
        expected = ascending_by_fraction[replay["fraction_float_hex"]]
        if any(replay[field] != expected[field] for field in reproducibility_fields):
            raise BoundaryLayerSmokeError(
                "physical-schedule homotopy ascending/descending replay differs"
            )
    zero_record = ascending_by_fraction[0.0.hex()]
    if (
        not isinstance(expected_minimum_growth_quality_sha256, str)
        or zero_record["quality"]["quality_sha256"]
        != expected_minimum_growth_quality_sha256.casefold()
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy minimum-growth quality anchor differs"
        )
    one_record = ascending_by_fraction[1.0.hex()]
    passed = [
        record["fraction"]
        for record in ascending_scan
        if record["quality"]["status"] == "PASS"
    ]
    failed = [
        record["fraction"]
        for record in ascending_scan
        if record["quality"]["status"] != "PASS"
    ]
    return {
        "ascending_scan": ascending_scan,
        "descending_replay": descending_replay,
        "final_quality": one_record["quality"],
        "maximum_tested_pass_fraction_float_hex": (
            max(passed).hex() if passed else None
        ),
        "first_tested_fail_fraction_float_hex": (
            min(failed).hex() if failed else None
        ),
        "fraction_count": len(fractions),
        "first_layer_height_float_hex": first_height_hex,
        "applied_root_count": len(roots),
        "updated_chain_node_count": len(updated_nodes),
    }


def _run_physical_schedule_fixed_direction_homotopy(
    gmsh: Any,
    *,
    approval: Mapping[str, Any],
    schedule_evidence: Mapping[str, Any],
    root_coordinates: Mapping[int, Sequence[float]],
    runtime_directions: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    bad_source_triangles: Sequence[Sequence[int]],
    bad_source_triangle_wall_fingerprints: Mapping[
        tuple[int, int, int], str
    ],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    source_prisms: Sequence[Mapping[str, Any]],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Apply approved directions and scan physical schedule homotopy only."""

    contexts = approval.get("root_contexts")
    changed_records = approval.get("changed_roots")
    if not isinstance(contexts, list) or not isinstance(changed_records, list):
        raise BoundaryLayerSmokeError("fixed-direction homotopy roots are missing")
    expected_contexts = {
        str(record["root_coordinate_sha256"]): record for record in contexts
    }
    if (
        len(expected_contexts) != len(contexts)
        or len(contexts) != int(approval["candidate_root_count"])
        or len(changed_records) != int(approval["changed_root_count"])
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction homotopy consensus root counts differ"
        )
    normalized_bad_triangles = sorted(
        {
            tuple(sorted(int(value) for value in triangle))
            for triangle in bad_source_triangles
        }
    )
    if (
        not normalized_bad_triangles
        or len(normalized_bad_triangles) != len(bad_source_triangles)
        or set(bad_source_triangle_wall_fingerprints)
        != set(normalized_bad_triangles)
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction homotopy A-state bad-triangle lineage is invalid"
        )
    authorization = _resolve_fixed_direction_replay_consensus_subgraph(
        expected_contexts=expected_contexts,
        expected_candidate_root_count=int(approval["candidate_root_count"]),
        root_coordinates=root_coordinates,
        chains=chains,
        source_triangles=source_triangles,
        current_bad_source_triangles=normalized_bad_triangles,
        source_triangle_wall_fingerprints=source_triangle_wall_fingerprints,
    )
    candidate_roots = authorization["candidate_roots"]
    runtime_by_stable = authorization["runtime_by_stable"]
    runtime_triangle_by_stable = authorization["runtime_triangle_by_stable"]
    approved_triangle_walls = authorization[
        "approved_triangle_wall_fingerprints"
    ]
    fresh_topology = _rebuild_owner_free_interaction_topology(
        gmsh,
        base_runtime_components=authorization["base_runtime_components"],
        base_stable_components=authorization["base_stable_components"],
        root_coordinates=root_coordinates,
        chains=chains,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
    )
    expected_topology_sha256 = str(
        approval["consensus"]["interaction_topology_sha256"]
    )
    if (
        fresh_topology["interaction_topology"]["interaction_topology_sha256"]
        != expected_topology_sha256
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction homotopy interaction topology differs from consensus"
        )
    runtime_context_records: list[dict[str, Any]] = []
    for root_id, expected in sorted(expected_contexts.items()):
        root = runtime_by_stable[root_id]
        incident_ids = sorted(
            triangle_id
            for triangle_id, triangle in runtime_triangle_by_stable.items()
            if root in triangle
        )
        walls = sorted(
            {
                str(approved_triangle_walls[triangle])
                for triangle_id, triangle in runtime_triangle_by_stable.items()
                if triangle_id in incident_ids
            }
        )
        runtime_context: dict[str, Any] = {
            "root_coordinate_sha256": root_id,
            "incident_bad_source_triangle_sha256": incident_ids,
            "incident_wall_surface_fingerprints": walls,
            "base_component_sha256": str(
                fresh_topology["base_component_by_root"][root]
            ),
            "interaction_component_sha256": str(
                fresh_topology["interaction_component_by_root"][root]
            ),
            "changed": bool(expected["changed"]),
        }
        runtime_context["root_context_sha256"] = _canonical_hash(runtime_context)
        if runtime_context != expected:
            raise BoundaryLayerSmokeError(
                "fixed-direction homotopy root context differs from consensus"
            )
        runtime_context_records.append(runtime_context)

    selected_directions = {
        int(root): tuple(float(value) for value in direction)
        for root, direction in runtime_directions.items()
    }
    changed_evidence: list[dict[str, Any]] = []
    changed_runtime_roots: set[int] = set()
    for record in changed_records:
        root_id = str(record["root_coordinate_sha256"])
        root = runtime_by_stable[root_id]
        expected_original = tuple(
            float.fromhex(str(value))
            for value in record["original_direction_float_hex"]
        )
        actual_original = tuple(float(value) for value in runtime_directions[root])
        maximum_difference = max(
            abs(actual - expected)
            for actual, expected in zip(actual_original, expected_original)
        )
        if maximum_difference > _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE:
            raise BoundaryLayerSmokeError(
                "fixed-direction homotopy original direction differs from consensus"
            )
        selected = tuple(
            float.fromhex(str(value))
            for value in record["selected_direction_float_hex"]
        )
        unit = _unit_vector(selected)
        if unit is None or any(
            not math.isclose(a, b, rel_tol=1.0e-12, abs_tol=1.0e-12)
            for a, b in zip(unit, selected)
        ):
            raise BoundaryLayerSmokeError(
                "fixed-direction homotopy selected direction is not a unit vector"
            )
        selected_directions[root] = selected
        changed_runtime_roots.add(root)
        changed_evidence.append(
            {
                "root_coordinate_sha256": root_id,
                "root_context_sha256": str(record["root_context_sha256"]),
                "original_direction_float_hex": list(
                    record["original_direction_float_hex"]
                ),
                "selected_direction_float_hex": list(
                    record["selected_direction_float_hex"]
                ),
                "application": (
                    "ABSOLUTE_ORIGIN_PLUS_APPROVED_DIRECTION_TIMES_"
                    "HOMOTOPY_SCHEDULE"
                ),
            }
        )
    expected_changed_ids = {
        root_id
        for root_id, context in expected_contexts.items()
        if context["changed"] is True
    }
    if (
        {str(record["root_coordinate_sha256"]) for record in changed_records}
        != expected_changed_ids
        or len(changed_runtime_roots) != int(approval["changed_root_count"])
    ):
        raise BoundaryLayerSmokeError(
            "fixed-direction homotopy changed-root coverage is incomplete"
        )
    endpoint = schedule_evidence.get("physical_schedule_homotopy_endpoint")
    authoritative_schedules = schedule_evidence.get(
        "authoritative_physical_surface_schedules"
    )
    if not isinstance(endpoint, Mapping) or not isinstance(
        authoritative_schedules, Mapping
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule homotopy schedule evidence is incomplete"
        )
    source_quality = approval["source_minimum_growth_final_quality"]
    scan = _run_physical_schedule_homotopy_scan(
        gmsh,
        endpoint=endpoint,
        authoritative_surface_schedules=authoritative_schedules,
        expected_surface_fingerprints=sorted(authoritative_schedules),
        source_prisms=source_prisms,
        prism_volume_fingerprints=prism_volume_fingerprints,
        root_coordinates=root_coordinates,
        selected_directions=selected_directions,
        chains=chains,
        chain_origin=chain_origin,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        minimum_prism_scaled_jacobian=minimum_prism_scaled_jacobian,
        minimum_core_tetra_gamma=minimum_core_tetra_gamma,
        expected_minimum_growth_quality_sha256=str(
            source_quality["quality_sha256"]
        ),
    )
    all_sampled_states_pass = all(
        record["quality"]["status"] == "PASS"
        for record in scan["ascending_scan"]
    )
    final_status = "PASS" if all_sampled_states_pass else "INCOMPLETE"
    fractions = [float(value) for value in endpoint["fractions"]]
    unsigned_discovery: dict[str, Any] = {
        "schema": HOMOTOPY_DISCOVERY_SCHEMA,
        "status": final_status,
        "profile_complete": final_status == "PASS",
        "audit_only": True,
        "audit_variant": "physical_local_schedule_fixed_direction_homotopy",
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "runtime_mesh_tags_hardcoded": False,
        "source_bindings": {
            "direction_replay_approval_sha256": str(
                approval["approval_sha256"]
            ),
            "source_audit_manifest_sha256": [
                str(record["sha256"])
                for record in approval["source_audit_manifests"]
            ],
            "local_schedule_binding_sha256": str(
                schedule_evidence["binding_sha256"]
            ),
            "homotopy_endpoint_sha256": str(endpoint["endpoint_sha256"]),
        },
        "fixed_direction_contract": {
            "candidate_root_count": len(candidate_roots),
            "changed_root_count": len(changed_runtime_roots),
            "unchanged_root_count": len(candidate_roots) - len(changed_runtime_roots),
            "applied_direction_count": len(changed_runtime_roots),
            "direction_search_performed": False,
            "absolute_coordinate_replay": True,
            "changed_roots": changed_evidence,
        },
        "root_context_replay": {
            "expected_count": len(expected_contexts),
            "matched_count": len(runtime_context_records),
            "missing_count": 0,
            "duplicate_count": 0,
            "unexplained_count": 0,
            "contexts": runtime_context_records,
        },
        "homotopy_contract": {
            "endpoint": endpoint,
            "baseline_surface_schedules_sha256": str(
                schedule_evidence["baseline_surface_schedules_sha256"]
            ),
            "minimum_surface_schedules_sha256": str(
                schedule_evidence["schedule_feasibility_application"][
                    "candidate_surface_schedules_sha256"
                ]
            ),
            "fraction_count": len(fractions),
            "ascending_fraction_float_hex": [value.hex() for value in fractions],
            "descending_fraction_float_hex": [
                value.hex() for value in reversed(fractions)
            ],
            "absolute_schedule_rebuild": True,
            "ascending_descending_replay_required": True,
            "quality_monotonicity_assumed": False,
            "direction_search_performed": False,
        },
        "ascending_scan": scan["ascending_scan"],
        "descending_replay": scan["descending_replay"],
        "observed_counts": {
            "projected_3d_element_count": len(subdivided_prisms)
            + len(core_volume_records),
            "prism_element_count": len(subdivided_prisms),
            "core_element_count": len(core_volume_records),
            "core_tetra_count": sum(
                record["type"] == "Tetrahedron 4"
                for record in core_volume_records
            ),
            "homotopy_fraction_count": scan["fraction_count"],
            "ascending_quality_evaluation_count": scan["fraction_count"],
            "descending_quality_evaluation_count": scan["fraction_count"],
            "searched_direction_count": 0,
        },
        "final_quality": scan["final_quality"],
        "maximum_tested_pass_fraction_float_hex": scan[
            "maximum_tested_pass_fraction_float_hex"
        ],
        "first_tested_fail_fraction_float_hex": scan[
            "first_tested_fail_fraction_float_hex"
        ],
        "incomplete_reason": (
            None
            if final_status == "PASS"
            else "PHYSICAL_SCHEDULE_HOMOTOPY_QUALITY_FAILED"
        ),
    }
    return {
        **unsigned_discovery,
        "discovery_sha256": _canonical_hash(unsigned_discovery),
    }


def _frontier_collar_gate(
    *,
    search: Mapping[str, Any],
    search_quality: Mapping[str, Any],
    layer_count: int,
    minimum_frozen_prefix_layer_count: int,
) -> dict[str, Any]:
    """Classify the sole legal v5 collar handoff without mutating the mesh."""

    inventory = search.get("final_low_quality_prisms")
    records = (
        list(inventory.get("records", []))
        if isinstance(inventory, Mapping)
        and isinstance(inventory.get("records"), Sequence)
        and not isinstance(inventory.get("records"), (str, bytes))
        else []
    )
    triple = search.get("triple_pattern_refinement")
    triple_accepted = (
        int(triple.get("accepted_move_count", 0))
        if isinstance(triple, Mapping)
        else 0
    )
    residual_bad_records: list[dict[str, Any]] = []
    for record in records:
        roots = sorted(str(value) for value in record.get("root_coordinate_sha256", []))
        layer = record.get("layer_1_based")
        stable = {
            "source_triangle_sha256": str(
                record.get("source_triangle_sha256", "")
            ),
            "wall_surface_fingerprint": str(
                record.get("wall_surface_fingerprint", "")
            ),
            "root_coordinate_sha256": roots,
            "layer_index_1_based": layer,
        }
        residual_bad_records.append(
            {**stable, "prism_element_sha256": _canonical_hash(stable)}
        )
    residual_bad_records.sort(
        key=lambda record: record["prism_element_sha256"]
    )
    components = {
        str(record.get("interaction_component_sha256"))
        for record in records
        if record.get("interaction_component_sha256") is not None
    }
    triangles = {
        str(record.get("source_triangle_sha256"))
        for record in records
        if record.get("source_triangle_sha256") is not None
    }
    walls = {
        str(record.get("wall_surface_fingerprint"))
        for record in records
        if record.get("wall_surface_fingerprint") is not None
    }
    root_ids = {
        str(root_id)
        for record in records
        for root_id in record.get("root_coordinate_sha256", [])
    }
    raw_layers = [record.get("layer_1_based") for record in records]
    valid_layers = all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in raw_layers
    )
    layers = [int(value) for value in raw_layers] if valid_layers else []
    minimum_bad_layer = min(layers) if layers else None
    maximum_bad_layer = max(layers) if layers else None
    frozen_prefix = (
        minimum_bad_layer - 1 if minimum_bad_layer is not None else None
    )
    outer_tail = bool(
        layers
        and sorted(layers)
        == list(range(int(minimum_bad_layer), layer_count + 1))
        and maximum_bad_layer == layer_count
    )
    core_bad = int(search_quality.get("core_tetra_below_gamma_count", -1))
    nonpositive = int(search_quality.get("nonpositive_element_count", -1))
    nonfinite = int(search_quality.get("nonfinite_count", -1))
    triple_status = str(search.get("status", ""))
    triple_shape = bool(
        isinstance(triple, Mapping)
        and triple.get("performed") is True
        and int(triple.get("refined_component_count", 0)) == 1
    )
    shape_eligible = bool(
        len(components) == 1
        and len(triangles) == 1
        and len(root_ids) == 3
        and len(walls) == 1
        and records
        and triple_shape
    )
    if triple_status == "PASS" and not records:
        reason = "TRIPLE_ALREADY_PASS"
    elif core_bad != 0 or nonpositive != 0 or nonfinite != 0:
        reason = "RESIDUAL_QUALITY_GATE_FAILED"
    elif triple_status != "EXHAUSTED" or not shape_eligible:
        reason = "RESIDUAL_SCOPE_NOT_ELIGIBLE"
    elif not outer_tail:
        reason = "RESIDUAL_NOT_CONTIGUOUS_OUTER_TAIL"
    elif frozen_prefix is None or frozen_prefix < minimum_frozen_prefix_layer_count:
        reason = "FROZEN_PREFIX_TOO_SHORT"
    else:
        reason = "ELIGIBLE_CONTIGUOUS_OUTER_TAIL"
    eligible = reason == "ELIGIBLE_CONTIGUOUS_OUTER_TAIL"
    return {
        "eligible": eligible,
        "reason": reason,
        "triple_status": triple_status,
        "triple_accepted_move_count": triple_accepted,
        "residual_component_count": len(components),
        "residual_source_triangle_count": len(triangles),
        "residual_root_count": len(root_ids),
        "residual_wall_count": len(walls),
        "residual_prism_count": len(records),
        "minimum_bad_layer_1_based": minimum_bad_layer,
        "maximum_bad_layer_1_based": maximum_bad_layer,
        "outer_tail_contiguous": outer_tail,
        "layer_count": layer_count,
        "frozen_prefix_last_layer_1_based": frozen_prefix,
        "minimum_frozen_prefix_layer_count": (
            minimum_frozen_prefix_layer_count
        ),
        "core_tetra_below_gamma_count": core_bad,
        "nonpositive_element_count": nonpositive,
        "nonfinite_count": nonfinite,
        "target_wall_surface_fingerprint": (
            next(iter(walls)) if len(walls) == 1 else None
        ),
        "target_source_triangle_sha256": (
            next(iter(triangles)) if len(triangles) == 1 else None
        ),
        "seed_root_coordinate_sha256": (
            sorted(root_ids) if len(root_ids) == 3 else None
        ),
        "residual_bad_records": residual_bad_records,
        "residual_bad_records_sha256": _canonical_hash(
            residual_bad_records
        ),
    }


def _frontier_collar_graph(
    *,
    target_wall_surface_fingerprint: str,
    seed_root_coordinate_sha256: Sequence[str],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    root_coordinates: Mapping[int, Sequence[float]],
) -> tuple[dict[str, Any], dict[int, int]]:
    """Build the wall-restricted stable source-triangle edge graph."""

    seed_ids = sorted(str(value) for value in seed_root_coordinate_sha256)
    normalized_wall = str(target_wall_surface_fingerprint).casefold()
    wall_triangles: set[tuple[int, int, int]] = set()
    for raw_triangle in source_triangles:
        triangle = tuple(sorted(int(value) for value in raw_triangle))
        if len(triangle) != 3 or len(set(triangle)) != 3:
            raise BoundaryLayerSmokeError(
                "frontier collar source triangle is invalid"
            )
        wall = source_triangle_wall_fingerprints.get(triangle)
        if wall is not None and str(wall).casefold() == normalized_wall:
            wall_triangles.add(triangle)
    if not wall_triangles:
        raise BoundaryLayerSmokeError(
            "frontier collar target wall graph is empty"
        )
    graph_roots = {root for triangle in wall_triangles for root in triangle}
    if any(root not in root_coordinates for root in graph_roots):
        raise BoundaryLayerSmokeError(
            "frontier collar graph root coordinate is missing"
        )
    stable_to_runtime: dict[str, int] = {}
    for root in sorted(graph_roots):
        root_id = _stable_root_id(root_coordinates[root])
        if root_id in stable_to_runtime:
            raise BoundaryLayerSmokeError(
                "frontier collar stable wall-root identity is ambiguous"
            )
        stable_to_runtime[root_id] = int(root)
    if len(seed_ids) != 3 or any(value not in stable_to_runtime for value in seed_ids):
        raise BoundaryLayerSmokeError(
            "frontier collar seed root identity is incomplete"
        )
    adjacency: dict[int, set[int]] = {root: set() for root in graph_roots}
    edge_runtime: set[tuple[int, int]] = set()
    for triangle in wall_triangles:
        for index, first in enumerate(triangle):
            for second in triangle[index + 1 :]:
                edge = tuple(sorted((first, second)))
                edge_runtime.add(edge)
                adjacency[first].add(second)
                adjacency[second].add(first)
    seed_runtime = [stable_to_runtime[value] for value in seed_ids]
    if any(root not in graph_roots for root in seed_runtime):
        raise BoundaryLayerSmokeError(
            "frontier collar seed is outside the target wall graph"
        )
    distances: dict[int, int] = {root: 0 for root in seed_runtime}
    frontier = sorted(seed_runtime)
    while frontier:
        root = frontier.pop(0)
        for neighbour in sorted(adjacency[root]):
            if neighbour in distances:
                continue
            distances[neighbour] = distances[root] + 1
            frontier.append(neighbour)
    if set(distances) != graph_roots:
        raise BoundaryLayerSmokeError(
            "frontier collar target wall graph is disconnected from its seeds"
        )
    root_ids = sorted(_stable_root_id(root_coordinates[root]) for root in graph_roots)
    triangle_records = sorted(
        (
            {
                "source_triangle_sha256": _stable_triangle_id(
                    triangle, root_coordinates
                ),
                "wall_surface_fingerprint": normalized_wall,
                "root_coordinate_sha256": sorted(
                    _stable_root_id(root_coordinates[root])
                    for root in triangle
                ),
            }
            for triangle in wall_triangles
        ),
        key=lambda record: record["source_triangle_sha256"],
    )
    triangle_ids = [
        record["source_triangle_sha256"] for record in triangle_records
    ]
    edge_records: list[dict[str, Any]] = []
    for first, second in edge_runtime:
        ids = sorted(
            (
                _stable_root_id(root_coordinates[first]),
                _stable_root_id(root_coordinates[second]),
            )
        )
        edge_records.append(
            {
                "edge_sha256": _canonical_hash(
                    {"root_coordinate_sha256": ids}
                ),
                "root_coordinate_sha256": ids,
            }
        )
    edge_records.sort(key=lambda record: record["edge_sha256"])
    roots_by_distance: dict[int, list[str]] = defaultdict(list)
    for root, distance in distances.items():
        roots_by_distance[int(distance)].append(
            _stable_root_id(root_coordinates[root])
        )
    distance_records = [
        {
            "distance": distance,
            "root_coordinate_sha256": sorted(roots_by_distance[distance]),
        }
        for distance in sorted(roots_by_distance)
    ]
    if [record["distance"] for record in distance_records] != list(
        range(len(distance_records))
    ):
        raise BoundaryLayerSmokeError(
            "frontier collar graph distances are not contiguous"
        )
    graph_unsigned = {
        "scope": "target_wall_source_triangle_edge_graph",
        "wall_surface_fingerprint": normalized_wall,
        "root_coordinate_sha256": root_ids,
        "source_triangle_sha256": triangle_ids,
        "triangle_records": triangle_records,
        "edge_records": edge_records,
    }
    graph = {
        **graph_unsigned,
        "source_triangle_count": len(triangle_ids),
        "root_count": len(root_ids),
        "edge_count": len(edge_records),
        "seed_root_coordinate_sha256": seed_ids,
        "distance_records": distance_records,
        "graph_sha256": _canonical_hash(graph_unsigned),
        "distances_sha256": _canonical_hash(distance_records),
    }
    return graph, distances


def _frontier_collar_candidate_schedules(
    *,
    roots: Sequence[int],
    target_schedules: Mapping[int, Sequence[float]],
    distances: Mapping[int, int],
    ring_width: int,
    frozen_prefix_last_layer_1_based: int,
) -> tuple[dict[int, list[float]], list[int], float]:
    """Apply the PLAN collar formula to one independently rebuilt candidate."""

    if ring_width <= 0:
        raise BoundaryLayerSmokeError("frontier collar ring width is invalid")
    normalized_roots = sorted(int(value) for value in roots)
    schedules = {
        root: [float(value) for value in target_schedules[root]]
        for root in normalized_roots
    }
    layer_counts = {len(values) for values in schedules.values()}
    first_heights = {values[0].hex() for values in schedules.values() if values}
    if (
        len(layer_counts) != 1
        or not layer_counts
        or len(first_heights) != 1
        or frozen_prefix_last_layer_1_based <= 0
        or frozen_prefix_last_layer_1_based >= next(iter(layer_counts))
    ):
        raise BoundaryLayerSmokeError(
            "frontier collar schedule coverage is invalid"
        )
    first_height = schedules[normalized_roots[0]][0]
    layer_count = next(iter(layer_counts))
    k = frozen_prefix_last_layer_1_based
    affected: list[int] = []
    reduction_terms: list[float] = []
    for root in normalized_roots:
        distance = distances.get(root)
        if distance is None or distance >= ring_width:
            continue
        weight = max(0.0, 1.0 - float(distance) / float(ring_width))
        baseline = schedules[root]
        candidate = list(baseline)
        b_k = baseline[k - 1]
        a_k = k * first_height
        for layer in range(k + 1, layer_count + 1):
            index = layer - 1
            a_i = layer * first_height
            b_i = baseline[index]
            c_i = (
                b_k
                + (a_i - a_k)
                + (1.0 - weight)
                * ((b_i - b_k) - (a_i - a_k))
            )
            if not math.isfinite(c_i) or c_i < a_i or c_i > b_i:
                raise BoundaryLayerSmokeError(
                    "frontier collar candidate amplified or undercut a schedule"
                )
            candidate[index] = c_i
            reduction_terms.append((b_i - c_i) * (b_i - c_i))
        if (
            candidate[0].hex() != baseline[0].hex()
            or candidate[:k] != baseline[:k]
            or any(right <= left for left, right in zip(candidate, candidate[1:]))
        ):
            raise BoundaryLayerSmokeError(
                "frontier collar candidate violated its frozen schedule prefix"
            )
        if candidate != baseline:
            schedules[root] = candidate
        affected.append(root)
    if not affected:
        raise BoundaryLayerSmokeError(
            "frontier collar candidate changed no root schedule"
        )
    return schedules, sorted(affected), math.sqrt(math.fsum(reduction_terms))


def _frontier_collar_coordinate_sha256(
    coordinates: Mapping[int, Sequence[float]],
    *,
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    maximum_layer: int | None = None,
) -> str:
    """Hash full or frozen-prefix chain coordinates by stable root identity."""

    digest = hashlib.sha256()
    digest.update(
        (
            "cfdpipe-homotopy-root-sha-layer-float-hex-v1\n"
            if maximum_layer is None
            else "cfdpipe-collar-inner-root-sha-layer-float-hex-v1\n"
        ).encode("ascii")
    )
    ordered_roots = sorted(
        (int(root) for root in roots),
        key=lambda root: _stable_root_id(root_coordinates[root]),
    )
    for root in ordered_roots:
        root_id = _stable_root_id(root_coordinates[root])
        chain = list(chains[root])
        last = len(chain) - 1 if maximum_layer is None else maximum_layer
        if last < 0 or last >= len(chain):
            raise BoundaryLayerSmokeError(
                "frontier collar coordinate subset exceeds a layer chain"
            )
        for layer, node in enumerate(chain[: last + 1]):
            try:
                values = [float(value) for value in coordinates[int(node)]]
            except KeyError as error:
                raise BoundaryLayerSmokeError(
                    "frontier collar coordinate evidence is incomplete"
                ) from error
            if len(values) != 3 or any(not math.isfinite(value) for value in values):
                raise BoundaryLayerSmokeError(
                    "frontier collar coordinate evidence is non-finite"
                )
            digest.update(
                (
                    root_id
                    + "|"
                    + str(layer)
                    + "|"
                    + "|".join(value.hex() for value in values)
                    + "\n"
                ).encode("ascii")
            )
    return digest.hexdigest()


def _frontier_collar_node_set_sha256(
    coordinates: Mapping[int, Sequence[float]],
    nodes: Sequence[int],
) -> str:
    coordinate_multiset: list[list[str]] = []
    for node in sorted(int(value) for value in nodes):
        values = [float(value) for value in coordinates[node]]
        if len(values) != 3 or any(not math.isfinite(value) for value in values):
            raise BoundaryLayerSmokeError(
                "frontier collar non-target coordinate is non-finite"
            )
        coordinate_multiset.append([value.hex() for value in values])
    coordinate_multiset.sort()
    return _canonical_hash(
        {"coordinate_float_hex_multiset": coordinate_multiset}
    )


def _frontier_collar_topology(
    *,
    all_coordinates: Mapping[int, Sequence[float]],
    root_coordinates: Mapping[int, Sequence[float]],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
) -> dict[str, Any]:
    """Precompute stable cross-wall root/triangle/element closure records."""

    triangle_records: list[dict[str, Any]] = []
    triangle_records_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw_triangle in source_triangles:
        triangle = tuple(sorted(int(value) for value in raw_triangle))
        try:
            wall = str(source_triangle_wall_fingerprints[triangle]).casefold()
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "frontier collar source-triangle wall lineage is incomplete"
            ) from error
        record = {
            "source_triangle_sha256": _stable_triangle_id(
                triangle, root_coordinates
            ),
            "wall_surface_fingerprint": wall,
            "root_coordinate_sha256": sorted(
                _stable_root_id(root_coordinates[root]) for root in triangle
            ),
        }
        triangle_records.append(record)
        for root in triangle:
            triangle_records_by_root[root].append(record)
    triangle_records.sort(key=lambda record: record["source_triangle_sha256"])

    def stable_node_sha256(node: int) -> str:
        mapped = chain_origin.get(int(node))
        if mapped is not None:
            root, layer = mapped
            return _canonical_hash(
                {
                    "root_coordinate_sha256": _stable_root_id(
                        root_coordinates[int(root)]
                    ),
                    "layer_index_1_based": int(layer),
                }
            )
        try:
            values = [float(value) for value in all_coordinates[int(node)]]
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "frontier collar fixed connectivity node is missing"
            ) from error
        return _stable_root_id(values)

    prism_records: list[dict[str, Any]] = []
    prism_records_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw in subdivided_prisms:
        mapped = [chain_origin.get(int(node)) for node in raw["nodes"]]
        if any(value is None for value in mapped):
            raise BoundaryLayerSmokeError(
                "frontier collar Prism6 closure is not chain-mappable"
            )
        roots = sorted({int(value[0]) for value in mapped if value is not None})
        levels = Counter(int(value[1]) for value in mapped if value is not None)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError(
                "frontier collar Prism6 closure lineage is invalid"
            )
        try:
            wall = str(
                prism_volume_fingerprints[int(raw["entity"])]
            ).casefold()
        except KeyError as error:
            raise BoundaryLayerSmokeError(
                "frontier collar Prism6 wall lineage is missing"
            ) from error
        stable = {
            "wall_surface_fingerprint": wall,
            "source_triangle_sha256": _stable_triangle_id(
                roots, root_coordinates
            ),
            "root_coordinate_sha256": sorted(
                _stable_root_id(root_coordinates[root]) for root in roots
            ),
            "layer_index_1_based": min(levels) + 1,
        }
        record = {
            **stable,
            "prism_element_sha256": _canonical_hash(stable),
        }
        prism_records.append(record)
        for root in roots:
            prism_records_by_root[root].append(record)
    prism_records.sort(key=lambda record: record["prism_element_sha256"])

    core_records: list[dict[str, Any]] = []
    core_records_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw in core_volume_records:
        touched_roots = sorted(
            {
                int(chain_origin[int(node)][0])
                for node in raw["nodes"]
                if int(node) in chain_origin
            }
        )
        if str(raw["type"]) != "Tetrahedron 4" or len(raw["nodes"]) != 4:
            raise BoundaryLayerSmokeError(
                "frontier collar core closure is not pure Tetrahedron 4"
            )
        stable = {
            "element_type": "Tetrahedron 4",
            "node_sha256": sorted(
                stable_node_sha256(int(node)) for node in raw["nodes"]
            ),
        }
        record = {
            **stable,
            "core_element_sha256": _canonical_hash(stable),
        }
        core_records.append(record)
        for root in touched_roots:
            core_records_by_root[root].append(record)
    core_records.sort(key=lambda record: record["core_element_sha256"])
    connectivity_unsigned = {
        "source_triangle_records": triangle_records,
        "prism_element_sha256": [
            record["prism_element_sha256"] for record in prism_records
        ],
        "core_element_sha256": [
            record["core_element_sha256"] for record in core_records
        ],
    }
    return {
        "connectivity_sha256": _canonical_hash(connectivity_unsigned),
        "triangle_records": triangle_records,
        "triangle_records_by_root": triangle_records_by_root,
        "prism_records_by_root": prism_records_by_root,
        "core_records_by_root": core_records_by_root,
    }


def _run_frontier_schedule_collar(
    gmsh: Any,
    *,
    endpoint: Mapping[str, Any],
    search: Mapping[str, Any],
    search_quality: Mapping[str, Any],
    roots: Sequence[int],
    root_coordinates: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    target_schedules: Mapping[int, Sequence[float]],
    selected_directions: Mapping[int, Sequence[float]],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    source_triangle_normals: Mapping[
        tuple[int, int, int], Sequence[float]
    ],
    incident_normals: Mapping[int, Sequence[Sequence[float]]],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
    prism_element_tags: Sequence[int],
    core_element_tags: Sequence[int],
    core_tetra_element_tags: Sequence[int],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
    minimum_cone_margin: float,
    minimum_changed_direction_margin: float,
    apply_state: Callable[
        [Mapping[int, Sequence[float]], Mapping[int, Sequence[float]]],
        dict[str, Any],
    ],
    non_target_nodes: Sequence[int],
) -> tuple[dict[str, Any], dict[int, list[float]], dict[str, Any] | None, dict[str, Any]]:
    """Evaluate the seven absolute v5 root-schedule collar candidates."""

    if endpoint.get("schema") != COLLAR_REFINEMENT_ENDPOINT_SCHEMA:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier collar endpoint schema differs"
        )
    normalized_roots = sorted(int(value) for value in roots)
    layer_counts = {len(target_schedules[root]) for root in normalized_roots}
    if (
        not normalized_roots
        or len(layer_counts) != 1
        or next(iter(layer_counts)) != int(endpoint["layer_count"])
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier collar layer coverage differs"
        )
    layer_count = next(iter(layer_counts))
    minimum_frozen = int(endpoint["minimum_frozen_inner_layer_count"])
    ring_widths = [int(value) for value in endpoint["collar_ring_widths"]]
    if ring_widths != [1, 2, 4, 8, 16, 32, 64]:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier collar ring widths differ"
        )
    pattern_count = int(search.get("quality_evaluation_count", 0))
    maximum_total = int(endpoint["maximum_quality_evaluations"])
    gate = _frontier_collar_gate(
        search=search,
        search_quality=search_quality,
        layer_count=layer_count,
        minimum_frozen_prefix_layer_count=minimum_frozen,
    )

    def evaluation_evidence(count: int) -> dict[str, Any]:
        return {
            "pattern_quality_evaluation_count": pattern_count,
            "collar_quality_evaluation_start_index_exclusive": pattern_count,
            "collar_quality_evaluation_count": count,
            "collar_quality_evaluation_end_index_inclusive": (
                pattern_count + count
            ),
            "maximum_collar_quality_evaluations": len(ring_widths),
            "total_quality_evaluation_count": pattern_count + count,
            "maximum_total_quality_evaluations": maximum_total,
            "within_cap": pattern_count + count <= maximum_total,
        }

    normalized_target = {
        root: [float(value) for value in target_schedules[root]]
        for root in normalized_roots
    }
    if not gate["eligible"]:
        skipped = {
            "schema": _COLLAR_REFINEMENT_SELECTION_SCHEMA,
            "status": "SKIPPED",
            "gate": gate,
            "handoff": None,
            "schedule": None,
            "graph": None,
            "ring_widths": ring_widths,
            "candidate_records": [],
            "evaluation_evidence": evaluation_evidence(0),
            "selection": None,
            "final_state": None,
            "preservation": None,
        }
        return skipped, normalized_target, None, dict(search_quality)

    frozen_prefix = int(gate["frozen_prefix_last_layer_1_based"])
    triple_selection = search.get("triple_refinement_selection")
    triple_final = (
        triple_selection.get("final_state")
        if isinstance(triple_selection, Mapping)
        else None
    )
    if not isinstance(triple_final, Mapping):
        raise BoundaryLayerSmokeError(
            "frontier collar triple-final handoff is missing"
        )
    entry_coordinates = _nodes_from_gmsh(gmsh)
    actual_entry_coordinate_sha256 = _frontier_collar_coordinate_sha256(
        entry_coordinates,
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        chains=chains,
    )
    entry_inner_sha256 = _frontier_collar_coordinate_sha256(
        entry_coordinates,
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        chains=chains,
        maximum_layer=frozen_prefix,
    )
    entry_non_target_sha256 = _frontier_collar_node_set_sha256(
        entry_coordinates, non_target_nodes
    )
    entry_schedule_sha256 = _homotopy_root_schedule_sha256(
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        root_schedules=normalized_target,
    )
    actual_entry_direction_sha256 = _stable_direction_field_sha256(
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        directions=selected_directions,
    )
    objective_fields = list(endpoint["quality_objective"])
    entry_direction_sha256 = str(
        triple_final["selected_direction_field_sha256"]
    )
    entry_coordinate_sha256 = str(
        triple_final["selected_state_coordinate_sha256"]
    )
    entry_quality = dict(triple_final["selected_state_quality"])
    entry_objective = [
        float(value) for value in triple_final["selected_state_objective"]
    ]
    entry_objective_sha256 = str(
        triple_final["selected_state_objective_sha256"]
    )
    seed_root_ids = set(gate["seed_root_coordinate_sha256"])
    seed_runtime_roots = sorted(
        (
            root
            for root in normalized_roots
            if _stable_root_id(root_coordinates[root]) in seed_root_ids
        ),
        key=lambda root: _stable_root_id(root_coordinates[root]),
    )
    actual_triple_coordinate_records: list[dict[str, Any]] = []
    for root in seed_runtime_roots:
        root_id = _stable_root_id(root_coordinates[root])
        for layer, node in enumerate(chains[root]):
            values = [
                float(value) for value in entry_coordinates[int(node)]
            ]
            actual_triple_coordinate_records.append(
                {
                    "root_coordinate_sha256": root_id,
                    "layer": layer,
                    "coordinates_float_hex": [
                        value.hex() for value in values
                    ],
                }
            )
    actual_triple_coordinate_sha256 = _canonical_hash(
        {
            "encoding": "python-float-hex/root-sha/layer-v1",
            "records": actual_triple_coordinate_records,
        }
    )
    actual_triple_direction_sha256 = _stable_direction_field_sha256(
        roots=seed_runtime_roots,
        root_coordinates=root_coordinates,
        directions={
            root: selected_directions[root] for root in seed_runtime_roots
        },
    )
    if (
        len(seed_runtime_roots) != 3
        or actual_triple_coordinate_sha256 != entry_coordinate_sha256
        or actual_triple_direction_sha256 != entry_direction_sha256
        or
        entry_quality["quality_sha256"]
        != triple_final["selected_state_quality_sha256"]
        or entry_objective_sha256
        != _canonical_hash(
            {
                "quality_objective_fields": objective_fields,
                "objective": entry_objective,
            }
        )
    ):
        raise BoundaryLayerSmokeError(
            "frontier collar triple-final handoff hashes are stale"
        )
    handoff = {
        "without_reset_from_triple_final": True,
        "entry_schedule_sha256": entry_schedule_sha256,
        "entry_direction_field_sha256": entry_direction_sha256,
        "entry_coordinate_sha256": entry_coordinate_sha256,
        "entry_quality_sha256": str(entry_quality["quality_sha256"]),
        "entry_objective": entry_objective,
        "entry_objective_sha256": entry_objective_sha256,
    }
    graph, distances = _frontier_collar_graph(
        target_wall_surface_fingerprint=str(
            gate["target_wall_surface_fingerprint"]
        ),
        seed_root_coordinate_sha256=list(
            gate["seed_root_coordinate_sha256"]
        ),
        source_triangles=source_triangles,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
        root_coordinates=root_coordinates,
    )
    topology = _frontier_collar_topology(
        all_coordinates=entry_coordinates,
        root_coordinates=root_coordinates,
        source_triangles=source_triangles,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
        chain_origin=chain_origin,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
    )
    entry_residual = gate["residual_bad_records"]
    entry_lineage = {
        (
            str(record["wall_surface_fingerprint"]),
            str(record["source_triangle_sha256"]),
            int(record["layer_index_1_based"]),
        )
        for record in entry_residual
    }
    triangle_roots_by_id = {
        str(record["source_triangle_sha256"]): list(
            record["root_coordinate_sha256"]
        )
        for record in topology["triangle_records"]
    }
    first_height = normalized_target[normalized_roots[0]][0]
    schedule_evidence = {
        "formula": str(endpoint["outer_tail_cumulative_formula"]),
        "minimum_growth_formula": str(
            endpoint["minimum_growth_cumulative_formula"]
        ),
        "layer_count": layer_count,
        "first_layer_height_float_hex": first_height.hex(),
        "frozen_prefix_last_layer_1_based": frozen_prefix,
        "frozen_prefix_preserved": True,
        "directions_frozen": True,
        "connectivity_frozen": True,
        "no_amplification": True,
        "strictly_increasing": True,
    }
    candidate_records: list[dict[str, Any]] = []
    candidate_schedules: dict[int, dict[int, list[float]]] = {}
    for candidate_index, ring_width in enumerate(ring_widths, 1):
        schedules, affected_roots, reduction_l2 = (
            _frontier_collar_candidate_schedules(
                roots=normalized_roots,
                target_schedules=normalized_target,
                distances=distances,
                ring_width=ring_width,
                frozen_prefix_last_layer_1_based=frozen_prefix,
            )
        )
        affected_root_ids = sorted(
            _stable_root_id(root_coordinates[root])
            for root in affected_roots
        )
        affected_triangle_records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        affected_prism_records_by_id: dict[str, dict[str, Any]] = {}
        affected_core_records_by_id: dict[str, dict[str, Any]] = {}
        for root in affected_roots:
            for record in topology["triangle_records_by_root"].get(root, []):
                key = (
                    str(record["wall_surface_fingerprint"]),
                    str(record["source_triangle_sha256"]),
                )
                affected_triangle_records_by_key[key] = record
            for record in topology["prism_records_by_root"].get(root, []):
                affected_prism_records_by_id[
                    str(record["prism_element_sha256"])
                ] = record
            for record in topology["core_records_by_root"].get(root, []):
                affected_core_records_by_id[
                    str(record["core_element_sha256"])
                ] = record
        affected_triangle_records = sorted(
            affected_triangle_records_by_key.values(),
            key=lambda record: (
                record["source_triangle_sha256"],
                record["wall_surface_fingerprint"],
                record["root_coordinate_sha256"],
            ),
        )
        affected_prism_records = sorted(
            affected_prism_records_by_id.values(),
            key=lambda record: record["prism_element_sha256"],
        )
        affected_core_records = sorted(
            affected_core_records_by_id.values(),
            key=lambda record: record["core_element_sha256"],
        )
        affected_chain_node_records = []
        for root in affected_roots:
            root_id = _stable_root_id(root_coordinates[root])
            for layer, _node in enumerate(chains[root]):
                if layer <= frozen_prefix:
                    continue
                stable_node = {
                    "root_coordinate_sha256": root_id,
                    "layer_index_1_based": layer,
                }
                affected_chain_node_records.append(
                    {
                        **stable_node,
                        "chain_node_sha256": _canonical_hash(stable_node),
                    }
                )
        affected_chain_node_records.sort(
            key=lambda record: record["chain_node_sha256"]
        )
        affected_chain_node_sha256 = {
            record["chain_node_sha256"]
            for record in affected_chain_node_records
        }
        affected_core_records = [
            record
            for record in affected_core_records
            if set(record["node_sha256"]) & affected_chain_node_sha256
        ]
        schedule_records = []
        for root in affected_roots:
            baseline = normalized_target[root]
            candidate = schedules[root]
            distance = int(distances[root])
            weight = max(
                0.0, 1.0 - float(distance) / float(ring_width)
            )
            schedule_records.append(
                {
                    "root_coordinate_sha256": _stable_root_id(
                        root_coordinates[root]
                    ),
                    "distance": distance,
                    "spatial_weight_float_hex": weight.hex(),
                    "entry_cumulative_distance_float_hex": [
                        value.hex() for value in baseline
                    ],
                    "exit_cumulative_distance_float_hex": [
                        value.hex() for value in candidate
                    ],
                }
            )
        schedule_records.sort(
            key=lambda record: record["root_coordinate_sha256"]
        )
        application = apply_state(schedules, selected_directions)
        if (
            application["direction_field_sha256"]
            != actual_entry_direction_sha256
        ):
            raise BoundaryLayerSmokeError(
                "frontier collar changed the selected direction field"
            )
        candidate_coordinates = _nodes_from_gmsh(gmsh)
        candidate_inner_sha256 = _frontier_collar_coordinate_sha256(
            candidate_coordinates,
            roots=normalized_roots,
            root_coordinates=root_coordinates,
            chains=chains,
            maximum_layer=frozen_prefix,
        )
        candidate_non_target_sha256 = _frontier_collar_node_set_sha256(
            candidate_coordinates, non_target_nodes
        )
        affected_runtime_nodes = {
            int(node)
            for root in affected_roots
            for node in chains[root][frozen_prefix + 1 :]
        }
        all_chain_nodes = {
            int(node)
            for root in normalized_roots
            for node in chains[root]
        }
        outside_closure_nodes = all_chain_nodes - affected_runtime_nodes
        outside_closure_changed_count = sum(
            tuple(entry_coordinates[node])
            != tuple(candidate_coordinates[node])
            for node in outside_closure_nodes
        )
        candidate_base_root_moved_count = sum(
            tuple(candidate_coordinates[root])
            != tuple(float(value) for value in root_coordinates[root])
            for root in normalized_roots
        )
        min_sj_by_tag: dict[int, float] = {}
        quality = _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=prism_element_tags,
            core_element_tags=core_element_tags,
            core_tetra_element_tags=core_tetra_element_tags,
            minimum_prism_scaled_jacobian=(
                minimum_prism_scaled_jacobian
            ),
            minimum_core_tetra_gamma=minimum_core_tetra_gamma,
            include_deficit_metrics=True,
            prism_scaled_jacobian_by_tag=min_sj_by_tag,
        )
        _bad_triangles, raw_low_records, low_aggregate = (
            _stable_low_quality_prism_lineage(
                gmsh,
                prism_element_tags=prism_element_tags,
                subdivided_prisms=subdivided_prisms,
                chain_origin=chain_origin,
                root_coordinates=root_coordinates,
                prism_volume_fingerprints=prism_volume_fingerprints,
                minimum_prism_scaled_jacobian=(
                    minimum_prism_scaled_jacobian
                ),
                prism_quality_values=min_sj_by_tag,
            )
        )
        if low_aggregate["count"] != quality[
            "prism_below_threshold_element_count"
        ]:
            raise BoundaryLayerSmokeError(
                "frontier collar cached low-quality lineage count differs"
            )
        low_records: list[dict[str, Any]] = []
        for record in raw_low_records:
            triangle_id = str(record["source_triangle_sha256"])
            try:
                low_roots = triangle_roots_by_id[triangle_id]
            except KeyError as error:
                raise BoundaryLayerSmokeError(
                    "frontier collar low-quality triangle left the source graph"
                ) from error
            stable_low = {
                "source_triangle_sha256": triangle_id,
                "wall_surface_fingerprint": str(
                    record["wall_surface_fingerprint"]
                ),
                "root_coordinate_sha256": low_roots,
                "layer_index_1_based": int(record["layer"]),
            }
            low_records.append(
                {
                    **stable_low,
                    "prism_element_sha256": _canonical_hash(stable_low),
                }
            )
        low_records.sort(key=lambda record: record["prism_element_sha256"])
        candidate_lineage = {
            (
                str(record["wall_surface_fingerprint"]),
                str(record["source_triangle_sha256"]),
                int(record["layer_index_1_based"]),
            )
            for record in low_records
        }
        new_lineage_count = len(candidate_lineage - entry_lineage)
        objective = list(
            _owner_free_quality_objective(
                quality,
                direction_change_penalty=0.0,
                quality_objective=objective_fields,
            )
        )
        objective_sha256 = _canonical_hash(
            {
                "quality_objective_fields": objective_fields,
                "objective": objective,
            }
        )
        preservation_safe = bool(
            candidate_inner_sha256 == entry_inner_sha256
            and candidate_non_target_sha256 == entry_non_target_sha256
            and outside_closure_changed_count == 0
            and candidate_base_root_moved_count == 0
        )
        selection_eligible = bool(
            new_lineage_count == 0
            and quality["core_tetra_below_gamma_count"] == 0
            and quality["nonpositive_element_count"] == 0
            and quality["nonfinite_count"] == 0
            and preservation_safe
        )
        unsafe_reasons = sorted(
            reason
            for reason, passed in {
                "lineage_contained": new_lineage_count == 0,
                "core_quality": quality["core_tetra_below_gamma_count"] == 0,
                "positive_elements": quality["nonpositive_element_count"] == 0,
                "finite_quality": quality["nonfinite_count"] == 0,
                "coordinate_preservation": preservation_safe,
            }.items()
            if not passed
        )
        if not selection_eligible:
            restored = apply_state(normalized_target, selected_directions)
            if (
                restored["root_schedule_sha256"] != entry_schedule_sha256
                or restored["coordinate_sha256"]
                != actual_entry_coordinate_sha256
                or restored["direction_field_sha256"]
                != actual_entry_direction_sha256
            ):
                raise BoundaryLayerSmokeError(
                    "frontier collar unsafe candidate S0 restore differs"
                )
        status = (
            "PASS"
            if quality["status"] == "PASS"
            and selection_eligible
            else ("FAIL" if selection_eligible else "UNSAFE")
        )
        affected_element_ids = sorted(
            [
                record["prism_element_sha256"]
                for record in affected_prism_records
            ]
            + [
                record["core_element_sha256"]
                for record in affected_core_records
            ]
        )
        low_aggregate_unsigned = dict(low_aggregate)
        low_aggregate_unsigned.pop("aggregate_sha256", None)
        low_aggregate_unsigned["records_sha256"] = _canonical_hash(
            low_records
        )
        low_aggregate = {
            **low_aggregate_unsigned,
            "aggregate_sha256": _canonical_hash(low_aggregate_unsigned),
        }
        schedule_records_sha256 = _canonical_hash(schedule_records)
        collar_schedule_proof_sha256 = _canonical_hash(
            {
                "frozen_prefix_last_layer_1_based": frozen_prefix,
                "ring_width": ring_width,
                "schedule_records": schedule_records,
            }
        )
        candidate_record = {
            "candidate_index_1_based": candidate_index,
            "ring_width": ring_width,
            "affected_root_count": len(affected_roots),
            "affected_triangle_count": len(affected_triangle_records),
            "affected_prism_element_count": len(affected_prism_records),
            "affected_core_element_count": len(affected_core_records),
            "affected_chain_node_count": len(affected_chain_node_records),
            "affected_root_sha256": _canonical_hash(affected_root_ids),
            "affected_triangle_sha256": _canonical_hash(
                sorted(
                    record["source_triangle_sha256"]
                    for record in affected_triangle_records
                )
            ),
            "affected_triangle_records_sha256": _canonical_hash(
                affected_triangle_records
            ),
            "affected_chain_node_records_sha256": _canonical_hash(
                affected_chain_node_records
            ),
            "affected_prism_records_sha256": _canonical_hash(
                affected_prism_records
            ),
            "affected_core_records_sha256": _canonical_hash(
                affected_core_records
            ),
            "affected_element_sha256": _canonical_hash(
                affected_element_ids
            ),
            "affected_triangle_records": affected_triangle_records,
            "affected_chain_node_records": affected_chain_node_records,
            "affected_prism_records": affected_prism_records,
            "affected_core_records": affected_core_records,
            "schedule_records": schedule_records,
            "schedule_records_sha256": schedule_records_sha256,
            "collar_schedule_proof_sha256": (
                collar_schedule_proof_sha256
            ),
            "entry_schedule_sha256": entry_schedule_sha256,
            "entry_direction_field_sha256": entry_direction_sha256,
            "entry_coordinate_sha256": entry_coordinate_sha256,
            "entry_quality_sha256": str(entry_quality["quality_sha256"]),
            "entry_objective_sha256": entry_objective_sha256,
            "exit_schedule_sha256": application["root_schedule_sha256"],
            "exit_direction_field_sha256": entry_direction_sha256,
            "exit_coordinate_sha256": application["coordinate_sha256"],
            "exit_quality_sha256": quality["quality_sha256"],
            "exit_objective_sha256": objective_sha256,
            "schedule_sha256": application["root_schedule_sha256"],
            "direction_field_sha256": entry_direction_sha256,
            "coordinate_sha256": application["coordinate_sha256"],
            "quality": quality,
            "quality_sha256": quality["quality_sha256"],
            "objective": objective,
            "objective_sha256": objective_sha256,
            "coordinate_reduction_l2_float_hex": reduction_l2.hex(),
            "quality_evaluation_index": pattern_count + candidate_index,
            "low_quality_aggregate": low_aggregate,
            "low_quality_records": low_records,
            "low_quality_records_sha256": _canonical_hash(low_records),
            "new_bad_lineage_count": new_lineage_count,
            "lineage_contained": new_lineage_count == 0,
            "inner_prefix_changed_node_count": (
                0
                if candidate_inner_sha256 == entry_inner_sha256
                else 1
            ),
            "outside_closure_changed_node_count": (
                outside_closure_changed_count
            ),
            "base_root_moved_count": candidate_base_root_moved_count,
            "connectivity_sha256": topology["connectivity_sha256"],
            "selection_eligible": selection_eligible,
            "unsafe_reasons": unsafe_reasons,
            "status": status,
        }
        candidate_records.append(candidate_record)
        candidate_schedules[candidate_index] = schedules
    if len(candidate_records) != len(ring_widths):
        raise BoundaryLayerSmokeError(
            "frontier collar did not evaluate all seven candidates"
        )
    passing = [
        record
        for record in candidate_records
        if record["status"] == "PASS"
    ]
    safe_candidates = [
        record
        for record in candidate_records
        if record["status"] != "UNSAFE"
    ]
    if not safe_candidates:
        reason_counts = Counter(
            reason
            for record in candidate_records
            for reason in record["unsafe_reasons"]
        )
        coupled_evidence: dict[str, Any] = {
            "status": "NOT_RUN",
            "seed_ring_width": 32,
        }
        try:
            seed_record = next(
                record
                for record in candidate_records
                if record["ring_width"] == 32
            )
            seed_schedules = candidate_schedules[
                int(seed_record["candidate_index_1_based"])
            ]
            apply_state(seed_schedules, selected_directions)
            seed_bad_triangles, seed_low_records, seed_low_aggregate = (
                _stable_low_quality_prism_lineage(
                    gmsh,
                    prism_element_tags=prism_element_tags,
                    subdivided_prisms=subdivided_prisms,
                    chain_origin=chain_origin,
                    root_coordinates=root_coordinates,
                    prism_volume_fingerprints=prism_volume_fingerprints,
                    minimum_prism_scaled_jacobian=(
                        minimum_prism_scaled_jacobian
                    ),
                )
            )
            seed_triangle_ids = {
                triangle: _stable_triangle_id(
                    triangle, root_coordinates
                )
                for triangle in seed_bad_triangles
            }
            seed_walls_by_id: dict[str, set[str]] = defaultdict(set)
            for record in seed_low_records:
                seed_walls_by_id[
                    str(record["source_triangle_sha256"])
                ].add(str(record["wall_surface_fingerprint"]))
            if any(len(values) != 1 for values in seed_walls_by_id.values()):
                raise BoundaryLayerSmokeError(
                    "coupled seed triangle has ambiguous wall lineage"
                )
            seed_triangle_walls = {
                triangle: next(
                    iter(seed_walls_by_id[triangle_id])
                )
                for triangle, triangle_id in seed_triangle_ids.items()
            }
            seed_components, seed_stable_components = (
                build_owner_free_components(
                    seed_bad_triangles,
                    triangle_stable_ids=seed_triangle_ids,
                    triangle_wall_fingerprints=seed_triangle_walls,
                    root_coordinates=root_coordinates,
                )
            )
            coupled_endpoint = (
                make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
                    schedule_endpoint_sha256=str(
                        endpoint["endpoint_sha256"]
                    )
                )
            )
            (
                coupled_search,
                _coupled_search_quality,
                coupled_selected,
            ) = _run_owner_free_component_direction_search(
                gmsh,
                components=seed_components,
                stable_components=seed_stable_components,
                bad_source_triangles=seed_bad_triangles,
                triangle_stable_ids=seed_triangle_ids,
                triangle_wall_fingerprints=seed_triangle_walls,
                prism_volume_fingerprints=prism_volume_fingerprints,
                source_triangle_normals=source_triangle_normals,
                incident_normals=incident_normals,
                original_directions=selected_directions,
                root_coordinates=root_coordinates,
                chains=chains,
                root_cumulative_heights=seed_schedules,
                subdivided_prisms=subdivided_prisms,
                core_volume_records=core_volume_records,
                minimum_prism_scaled_jacobian=(
                    minimum_prism_scaled_jacobian
                ),
                minimum_core_tetra_gamma=minimum_core_tetra_gamma,
                minimum_cone_margin=minimum_cone_margin,
                minimum_changed_direction_margin=(
                    minimum_changed_direction_margin
                ),
                endpoint=coupled_endpoint,
            )
            coupled_directions = dict(selected_directions)
            coupled_directions.update(coupled_selected)
            apply_state(seed_schedules, coupled_directions)
            coupled_quality = _owner_free_quality_snapshot(
                gmsh,
                prism_element_tags=prism_element_tags,
                core_element_tags=core_element_tags,
                core_tetra_element_tags=core_tetra_element_tags,
                minimum_prism_scaled_jacobian=(
                    minimum_prism_scaled_jacobian
                ),
                minimum_core_tetra_gamma=minimum_core_tetra_gamma,
                include_deficit_metrics=True,
            )
            coupled_evidence = {
                "status": coupled_quality["status"],
                "seed_ring_width": 32,
                "seed_low_quality_aggregate": seed_low_aggregate,
                "search": coupled_search,
                "quality": coupled_quality,
                "selected_direction_count": len(coupled_selected),
            }
            if coupled_quality["status"] == "PASS":
                if not isinstance(selected_directions, dict):
                    raise BoundaryLayerSmokeError(
                        "coupled PASS direction field is not mutable"
                    )
                selected_directions.update(coupled_selected)
                final_application = apply_state(
                    seed_schedules, selected_directions
                )
                final_coordinates = _nodes_from_gmsh(gmsh)
                final_non_target_sha256 = (
                    _frontier_collar_node_set_sha256(
                        final_coordinates, non_target_nodes
                    )
                )
                base_root_moved_count = sum(
                    tuple(final_coordinates[root])
                    != tuple(
                        float(value) for value in root_coordinates[root]
                    )
                    for root in normalized_roots
                )
                if (
                    final_non_target_sha256
                    != entry_non_target_sha256
                    or base_root_moved_count != 0
                    or coupled_quality[
                        "prism_below_threshold_element_count"
                    ]
                    != 0
                    or coupled_quality["nonpositive_element_count"] != 0
                    or coupled_quality["nonfinite_count"] != 0
                    or coupled_quality["core_tetra_below_gamma_count"] != 0
                    or float(
                        coupled_quality[
                            "minimum_prism_scaled_jacobian"
                        ]
                    )
                    < minimum_prism_scaled_jacobian
                ):
                    raise BoundaryLayerSmokeError(
                        "coupled PASS failed full-field acceptance"
                    )
                coupled_count = int(
                    coupled_search["quality_evaluation_count"]
                )
                total_count = pattern_count + len(candidate_records) + coupled_count
                if total_count > maximum_total:
                    raise BoundaryLayerSmokeError(
                        "coupled PASS exceeded total quality cap"
                    )
                coupled_unsigned = {
                    **coupled_evidence,
                    "seed_schedule_sha256": final_application[
                        "root_schedule_sha256"
                    ],
                    "final_direction_field_sha256": final_application[
                        "direction_field_sha256"
                    ],
                    "final_coordinate_sha256": final_application[
                        "coordinate_sha256"
                    ],
                }
                coupled_evidence = {
                    **coupled_unsigned,
                    "coupled_rescue_sha256": _canonical_hash(
                        coupled_unsigned
                    ),
                }
                evidence = {
                    "schema": (
                        "cfdpipe.coarse_schedule_frontier_coupled_"
                        "refinement_selection.v2"
                    ),
                    "status": "PASS",
                    "gate": gate,
                    "handoff": handoff,
                    "schedule": schedule_evidence,
                    "graph": graph,
                    "ring_widths": ring_widths,
                    "candidate_records": candidate_records,
                    "evaluation_evidence": {
                        **evaluation_evidence(len(candidate_records)),
                        "coupled_quality_evaluation_count": coupled_count,
                        "total_quality_evaluation_count": total_count,
                        "within_cap": total_count <= maximum_total,
                    },
                    "selection": {
                        "selection_basis": (
                            "ring32_schedule_then_bounded_direction_"
                            "triple_refinement"
                        ),
                        "seed_candidate_index_1_based": int(
                            seed_record["candidate_index_1_based"]
                        ),
                        "seed_ring_width": 32,
                        "selected_direction_count": len(
                            coupled_selected
                        ),
                    },
                    "final_state": {
                        "schedule_sha256": final_application[
                            "root_schedule_sha256"
                        ],
                        "direction_field_sha256": final_application[
                            "direction_field_sha256"
                        ],
                        "coordinate_sha256": final_application[
                            "coordinate_sha256"
                        ],
                        "quality_sha256": coupled_quality[
                            "quality_sha256"
                        ],
                        "quality": coupled_quality,
                    },
                    "preservation": {
                        "entry_non_target_coordinate_sha256": (
                            entry_non_target_sha256
                        ),
                        "final_non_target_coordinate_sha256": (
                            final_non_target_sha256
                        ),
                        "non_target_reproducible": True,
                        "base_root_moved_count": 0,
                        "connectivity_sha256": topology[
                            "connectivity_sha256"
                        ],
                    },
                    "coupled_rescue": coupled_evidence,
                }
                return (
                    evidence,
                    seed_schedules,
                    final_application,
                    coupled_quality,
                )
        except BaseException as coupled_error:
            coupled_evidence = {
                "status": "ERROR",
                "seed_ring_width": 32,
                "error": {
                    "type": type(coupled_error).__name__,
                    "message": str(coupled_error),
                    "traceback": "".join(
                        traceback.format_exception(
                            type(coupled_error),
                            coupled_error,
                            coupled_error.__traceback__,
                        )
                    ),
                },
            }
        error = BoundaryLayerSmokeError(
            "frontier collar has no lineage- and preservation-safe candidate: "
            f"unsafe_reason_counts={dict(sorted(reason_counts.items()))}"
        )
        error.evidence = {
            "schema": "cfdpipe.frontier_collar_candidate_rejection.v1",
            "status": "FAIL",
            "candidate_count": len(candidate_records),
            "quality_evaluation_count": pattern_count + len(candidate_records),
            "maximum_quality_evaluations": maximum_total,
            "within_evaluation_cap": (
                pattern_count + len(candidate_records)
                <= maximum_total
            ),
            "unsafe_reason_counts": dict(sorted(reason_counts.items())),
            "candidate_records": candidate_records,
            "coupled_diagnostic": coupled_evidence,
        }
        raise error

    def closure_rank(record: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            int(record["affected_root_count"]),
            int(record["affected_triangle_count"]),
            int(record["affected_prism_element_count"]),
            int(record["affected_core_element_count"]),
            int(record["affected_chain_node_count"]),
            float.fromhex(str(record["coordinate_reduction_l2_float_hex"])),
            int(record["candidate_index_1_based"]),
        )

    if passing:
        selected_record = min(passing, key=closure_rank)
        selection_basis = (
            "passing_candidate_minimum_affected_closure_then_coordinate_change"
        )
    else:
        selected_record = min(
            safe_candidates,
            key=lambda record: (
                tuple(float(value) for value in record["objective"]),
                closure_rank(record),
            ),
        )
        selection_basis = (
            "count_first_quality_then_minimum_affected_closure_then_coordinate_change"
        )
    selected_index = int(selected_record["candidate_index_1_based"])
    selected_schedules = candidate_schedules[selected_index]
    final_application = apply_state(selected_schedules, selected_directions)
    if (
        final_application["root_schedule_sha256"]
        != selected_record["exit_schedule_sha256"]
        or final_application["coordinate_sha256"]
        != selected_record["exit_coordinate_sha256"]
        or final_application["direction_field_sha256"]
        != actual_entry_direction_sha256
    ):
        raise BoundaryLayerSmokeError(
            "frontier collar selected candidate absolute replay differs"
        )
    final_coordinates = _nodes_from_gmsh(gmsh)
    final_inner_sha256 = _frontier_collar_coordinate_sha256(
        final_coordinates,
        roots=normalized_roots,
        root_coordinates=root_coordinates,
        chains=chains,
        maximum_layer=frozen_prefix,
    )
    final_non_target_sha256 = _frontier_collar_node_set_sha256(
        final_coordinates, non_target_nodes
    )
    selected_ring_width = int(selected_record["ring_width"])
    selected_affected_runtime_roots = {
        int(root)
        for root, distance in distances.items()
        if int(distance) < selected_ring_width
    }
    moved_nodes = {
        int(node)
        for root in normalized_roots
        if root in selected_affected_runtime_roots
        for node in chains[root][frozen_prefix + 1 :]
    }
    outside_nodes = sorted(
        {
            int(node)
            for root in normalized_roots
            for node in chains[root]
        }
        - moved_nodes
    )
    outside_changed_count = sum(
        tuple(entry_coordinates[node]) != tuple(final_coordinates[node])
        for node in outside_nodes
    )
    base_root_moved_count = sum(
        tuple(final_coordinates[root])
        != tuple(float(value) for value in root_coordinates[root])
        for root in normalized_roots
    )
    preservation = {
        "entry_inner_prefix_coordinate_sha256": entry_inner_sha256,
        "final_inner_prefix_coordinate_sha256": final_inner_sha256,
        "inner_prefix_reproducible": entry_inner_sha256 == final_inner_sha256,
        "entry_non_target_coordinate_sha256": entry_non_target_sha256,
        "final_non_target_coordinate_sha256": final_non_target_sha256,
        "non_target_reproducible": (
            entry_non_target_sha256 == final_non_target_sha256
        ),
        "entry_connectivity_sha256": topology["connectivity_sha256"],
        "final_connectivity_sha256": topology["connectivity_sha256"],
        "connectivity_reproducible": True,
    }
    if (
        not preservation["inner_prefix_reproducible"]
        or not preservation["non_target_reproducible"]
        or outside_changed_count != 0
        or base_root_moved_count != 0
    ):
        raise BoundaryLayerSmokeError(
            "frontier collar changed a frozen coordinate"
        )
    selection = {
        "selection_basis": selection_basis,
        "pass_candidate_count": len(passing),
        "selected_candidate_index_1_based": selected_index,
        "selected_ring_width": int(selected_record["ring_width"]),
        "selected_status": str(selected_record["status"]),
        "selected_schedule_sha256": selected_record["exit_schedule_sha256"],
        "selected_coordinate_sha256": selected_record["exit_coordinate_sha256"],
        "selected_quality_sha256": selected_record["exit_quality_sha256"],
        "selected_quality": selected_record["quality"],
        "selected_affected_root_count": selected_record[
            "affected_root_count"
        ],
        "selected_affected_triangle_count": selected_record[
            "affected_triangle_count"
        ],
        "selected_affected_prism_element_count": selected_record[
            "affected_prism_element_count"
        ],
        "selected_affected_core_element_count": selected_record[
            "affected_core_element_count"
        ],
        "selected_affected_chain_node_count": selected_record[
            "affected_chain_node_count"
        ],
        "selected_coordinate_reduction_l2_float_hex": selected_record[
            "coordinate_reduction_l2_float_hex"
        ],
    }
    final_quality = dict(selected_record["quality"])
    final_state = {
        "schedule_sha256": selected_record["exit_schedule_sha256"],
        "direction_field_sha256": entry_direction_sha256,
        "coordinate_sha256": selected_record["exit_coordinate_sha256"],
        "quality_sha256": selected_record["exit_quality_sha256"],
        "quality": final_quality,
    }
    evidence = {
        "schema": _COLLAR_REFINEMENT_SELECTION_SCHEMA,
        "status": "PASS" if selected_record["status"] == "PASS" else "EXHAUSTED",
        "gate": gate,
        "handoff": handoff,
        "schedule": schedule_evidence,
        "graph": graph,
        "ring_widths": ring_widths,
        "candidate_records": candidate_records,
        "evaluation_evidence": evaluation_evidence(len(candidate_records)),
        "selection": selection,
        "final_state": final_state,
        "preservation": preservation,
    }
    if not evidence["evaluation_evidence"]["within_cap"]:
        raise BoundaryLayerSmokeError(
            "frontier collar quality evaluation cap was exceeded"
        )
    return evidence, selected_schedules, final_application, final_quality


def _run_physical_schedule_first_frontier_direction_continuation(
    gmsh: Any,
    *,
    approval: Mapping[str, Any],
    schedule_evidence: Mapping[str, Any],
    root_coordinates: Mapping[int, Sequence[float]],
    runtime_directions: Mapping[int, Sequence[float]],
    chains: Mapping[int, Sequence[int]],
    minimum_growth_bad_source_triangles: Sequence[Sequence[int]],
    source_triangles: Sequence[Sequence[int]],
    source_triangle_wall_fingerprints: Mapping[tuple[int, int, int], str],
    source_triangle_normals: Mapping[
        tuple[int, int, int], Sequence[float]
    ],
    incident_normals: Mapping[int, Sequence[Sequence[float]]],
    source_prisms: Sequence[Mapping[str, Any]],
    chain_origin: Mapping[int, tuple[int, int]],
    subdivided_prisms: Sequence[Mapping[str, Any]],
    core_volume_records: Sequence[Mapping[str, Any]],
    prism_volume_fingerprints: Mapping[int, str],
    minimum_prism_scaled_jacobian: float,
    minimum_core_tetra_gamma: float,
    minimum_cone_margin: float,
    minimum_changed_direction_margin: float,
) -> dict[str, Any]:
    """Repair only the first sampled physical-schedule failure frontier."""

    continuation_endpoint = schedule_evidence.get(
        "schedule_direction_continuation_endpoint"
    )
    homotopy_endpoint = schedule_evidence.get(
        "physical_schedule_homotopy_endpoint"
    )
    authoritative_schedules = schedule_evidence.get(
        "authoritative_physical_surface_schedules"
    )
    if (
        not isinstance(continuation_endpoint, Mapping)
        or continuation_endpoint.get("schema")
        != SCHEDULE_DIRECTION_CONTINUATION_ENDPOINT_SCHEMA
        or not isinstance(homotopy_endpoint, Mapping)
        or not isinstance(authoritative_schedules, Mapping)
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier continuation evidence is incomplete"
        )
    prism_threshold = _finite(
        minimum_prism_scaled_jacobian,
        "frontier minimum Prism6 scaled Jacobian",
    )
    core_threshold = _finite(
        minimum_core_tetra_gamma,
        "frontier minimum core tetra gamma",
    )
    configured_thresholds = continuation_endpoint.get("quality_thresholds")
    if (
        not isinstance(configured_thresholds, Mapping)
        or float(
            configured_thresholds[
                "minimum_prism_scaled_jacobian_for_pass"
            ]
        )
        != prism_threshold
        or float(
            configured_thresholds[
                "minimum_core_tetra_gamma_for_pass"
            ]
        )
        != core_threshold
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier quality thresholds differ"
        )
    previous_fraction = float.fromhex(
        str(continuation_endpoint["previous_fraction_float_hex"])
    )
    target_fraction = float.fromhex(
        str(continuation_endpoint["target_fraction_float_hex"])
    )
    if previous_fraction != 0.0 or target_fraction != 2.0**-12:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier fractions are not the first frontier"
        )

    roots = sorted(int(value) for value in chains)
    prism_tags = sorted(int(record["tag"]) for record in subdivided_prisms)
    core_tags = sorted(int(record["tag"]) for record in core_volume_records)
    core_tetra_tags = sorted(
        int(record["tag"])
        for record in core_volume_records
        if record["type"] == "Tetrahedron 4"
    )
    if (
        not roots
        or len(prism_tags) != len(set(prism_tags))
        or len(core_tags) != len(set(core_tags))
        or not core_tetra_tags
        or set(prism_tags) & set(core_tags)
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier element inventory is invalid"
        )
    fixed = _prepare_fixed_approved_direction_field(
        gmsh,
        approval=approval,
        root_coordinates=root_coordinates,
        runtime_directions=runtime_directions,
        chains=chains,
        minimum_growth_bad_source_triangles=(
            minimum_growth_bad_source_triangles
        ),
        source_triangles=source_triangles,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
    )
    incoming_directions = {
        int(root): tuple(float(value) for value in direction)
        for root, direction in fixed["directions"].items()
    }

    updated_nodes = {
        int(node) for root in roots for node in chains[root][1:]
    }
    if len(updated_nodes) != sum(len(chains[root]) - 1 for root in roots):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier layer chains share a node"
        )
    parametric_coordinates_by_node: dict[int, list[float]] = {}
    for node in sorted(updated_nodes):
        existing = gmsh.model.mesh.getNode(node)
        if not isinstance(existing, Sequence) or len(existing) < 2:
            raise BoundaryLayerSmokeError(
                "physical-schedule frontier node metadata is incomplete"
            )
        coordinates = [float(value) for value in existing[0]]
        parameters = [float(value) for value in existing[1]]
        if len(coordinates) != 3 or any(
            not math.isfinite(value) for value in (*coordinates, *parameters)
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule frontier node metadata is non-finite"
            )
        parametric_coordinates_by_node[node] = parameters
    before_all_coordinates = _nodes_from_gmsh(gmsh)
    non_target_nodes = sorted(
        set(before_all_coordinates) - updated_nodes - set(roots)
    )
    non_target_before_sha256 = _frontier_collar_node_set_sha256(
        before_all_coordinates, non_target_nodes
    )

    def schedules_for_fraction(
        fraction: float,
    ) -> tuple[dict[int, list[float]], dict[str, Any]]:
        try:
            surface_schedules, interpolation = (
                interpolate_physical_schedule_homotopy(
                    authoritative_schedules,
                    homotopy_endpoint,
                    fraction,
                    expected_surface_fingerprints=sorted(
                        str(value) for value in authoritative_schedules
                    ),
                )
            )
        except CoarseDirectionReplayError as error:
            raise BoundaryLayerSmokeError(
                f"physical-schedule frontier interpolation failed: {error}"
            ) from error
        root_schedules, assignments = _assign_root_local_schedules(
            source_prisms,
            prism_volume_fingerprints=prism_volume_fingerprints,
            surface_schedules=surface_schedules,
            root_coordinates=root_coordinates,
        )
        if set(root_schedules) != set(roots):
            raise BoundaryLayerSmokeError(
                "physical-schedule frontier root schedules are incomplete"
            )
        return root_schedules, {
            "interpolation_evidence": interpolation,
            "root_schedule_assignments": assignments,
        }

    previous_schedules, previous_schedule_evidence = schedules_for_fraction(
        previous_fraction
    )
    target_schedules, target_schedule_evidence = schedules_for_fraction(
        target_fraction
    )

    def apply_state(
        schedules: Mapping[int, Sequence[float]],
        directions: Mapping[int, Sequence[float]],
    ) -> dict[str, Any]:
        return _apply_absolute_fraction_chain_state(
            gmsh,
            roots=roots,
            root_coordinates=root_coordinates,
            chains=chains,
            root_schedules=schedules,
            directions=directions,
            parametric_coordinates_by_node=parametric_coordinates_by_node,
        )

    def full_quality() -> dict[str, Any]:
        return _owner_free_quality_snapshot(
            gmsh,
            prism_element_tags=prism_tags,
            core_element_tags=core_tags,
            core_tetra_element_tags=core_tetra_tags,
            minimum_prism_scaled_jacobian=prism_threshold,
            minimum_core_tetra_gamma=core_threshold,
            include_deficit_metrics=True,
        )

    previous_application = apply_state(
        previous_schedules, incoming_directions
    )
    previous_quality = full_quality()
    if previous_quality["status"] != "PASS":
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier previous PASS anchor differs"
        )

    target_application = apply_state(target_schedules, incoming_directions)
    target_quality = full_quality()
    (
        target_bad_triangles,
        target_low_records,
        target_low_aggregate,
    ) = _stable_low_quality_prism_lineage(
        gmsh,
        prism_element_tags=prism_tags,
        subdivided_prisms=subdivided_prisms,
        chain_origin=chain_origin,
        root_coordinates=root_coordinates,
        prism_volume_fingerprints=prism_volume_fingerprints,
        minimum_prism_scaled_jacobian=prism_threshold,
    )
    target_observation = {
        "low_quality_prism_count": int(
            target_quality["prism_below_threshold_element_count"]
        ),
        "unique_source_triangle_count": len(target_bad_triangles),
        "wall_count": len(
            {
                record["wall_surface_fingerprint"]
                for record in target_low_records
            }
        ),
        "core_tetra_below_gamma_count": int(
            target_quality["core_tetra_below_gamma_count"]
        ),
        "nonpositive_element_count": int(
            target_quality["nonpositive_element_count"]
        ),
    }
    if (
        target_observation
        != dict(continuation_endpoint["target_observation"])
        or target_quality["core_tetra_below_gamma_count"] != 0
        or target_quality["nonpositive_element_count"] != 0
        or target_quality["nonfinite_count"] != 0
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier target observation differs"
        )

    target_triangle_ids = {
        triangle: _stable_triangle_id(triangle, root_coordinates)
        for triangle in target_bad_triangles
    }
    target_walls_by_id: dict[str, set[str]] = defaultdict(set)
    for record in target_low_records:
        target_walls_by_id[str(record["source_triangle_sha256"])].add(
            str(record["wall_surface_fingerprint"])
        )
    if any(len(values) != 1 for values in target_walls_by_id.values()):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier triangle has ambiguous wall lineage"
        )
    target_triangle_walls = {
        triangle: next(iter(target_walls_by_id[triangle_id]))
        for triangle, triangle_id in target_triangle_ids.items()
    }
    if any(
        source_triangle_wall_fingerprints.get(triangle)
        != target_triangle_walls[triangle]
        for triangle in target_bad_triangles
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier stable wall lineage changed"
        )
    try:
        runtime_components, stable_components = build_owner_free_components(
            target_bad_triangles,
            triangle_stable_ids=target_triangle_ids,
            triangle_wall_fingerprints=target_triangle_walls,
            root_coordinates=root_coordinates,
        )
    except CoarseComponentDirectionError as error:
        raise BoundaryLayerSmokeError(
            f"physical-schedule frontier components are invalid: {error}"
        ) from error
    candidate_roots = sorted(
        {root for component in runtime_components for root in component}
    )
    caps = continuation_endpoint.get("caps")
    if not isinstance(caps, Mapping):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier caps are missing"
        )
    precheck = _frontier_component_precheck(
        runtime_components,
        caps=caps,
    )
    if precheck["candidate_roots"] != candidate_roots:
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier precheck root order differs"
        )
    pattern_endpoint = (
        make_owner_free_direction_frontier_triple_refinement_pattern_endpoint(
            schedule_endpoint_sha256=str(
                continuation_endpoint["endpoint_sha256"]
            )
        )
    )
    search, search_quality, selected_candidates = (
        _run_owner_free_component_direction_search(
            gmsh,
            components=runtime_components,
            stable_components=stable_components,
            bad_source_triangles=target_bad_triangles,
            triangle_stable_ids=target_triangle_ids,
            triangle_wall_fingerprints=target_triangle_walls,
            prism_volume_fingerprints=prism_volume_fingerprints,
            source_triangle_normals=source_triangle_normals,
            incident_normals=incident_normals,
            original_directions=incoming_directions,
            root_coordinates=root_coordinates,
            chains=chains,
            root_cumulative_heights=target_schedules,
            subdivided_prisms=subdivided_prisms,
            core_volume_records=core_volume_records,
            minimum_prism_scaled_jacobian=prism_threshold,
            minimum_core_tetra_gamma=core_threshold,
            minimum_cone_margin=minimum_cone_margin,
            minimum_changed_direction_margin=(
                minimum_changed_direction_margin
            ),
            endpoint=pattern_endpoint,
        )
    )
    if int(search["quality_evaluation_count"]) > int(
        caps["maximum_quality_evaluations"]
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier quality cap was exceeded"
        )
    fine_refinement_selection = search.get("fine_refinement_selection")
    if not isinstance(fine_refinement_selection, Mapping):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier fine-refinement selection is missing"
        )
    triple_refinement_selection = search.get("triple_refinement_selection")
    if not isinstance(triple_refinement_selection, Mapping):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier triple-refinement selection is missing"
        )
    selected_directions = dict(incoming_directions)
    selected_directions.update(selected_candidates)
    selected_field_sha256 = _stable_direction_field_sha256(
        roots=roots,
        root_coordinates=root_coordinates,
        directions=selected_directions,
    )
    selected_changed_records = []
    for root in candidate_roots:
        incoming = _unit_vector(incoming_directions[root])
        selected = _unit_vector(selected_directions[root])
        if incoming is None or selected is None:
            raise BoundaryLayerSmokeError(
                "physical-schedule frontier selected direction is zero"
            )
        if tuple(round(value, 15) for value in incoming) == tuple(
            round(value, 15) for value in selected
        ):
            continue
        selected_changed_records.append(
            {
                "root_coordinate_sha256": _stable_root_id(
                    root_coordinates[root]
                ),
                "incoming_direction_float_hex": [
                    float(value).hex() for value in incoming
                ],
                "selected_direction_float_hex": [
                    float(value).hex() for value in selected
                ],
            }
        )
    selected_changed_records.sort(
        key=lambda record: record["root_coordinate_sha256"]
    )

    collar_endpoint = (
        make_owner_free_direction_frontier_collar_refinement_endpoint(
            schedule_endpoint_sha256=str(
                continuation_endpoint["endpoint_sha256"]
            )
        )
    )
    collar_contract = {
        key: value
        for key, value in collar_endpoint.items()
        if not key.endswith("_sha256")
    }
    if continuation_endpoint.get("collar_refinement_contract") != (
        collar_contract
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier collar contract differs"
        )
    (
        collar_refinement_selection,
        selected_target_schedules,
        _collar_final_application,
        collar_final_quality,
    ) = _run_frontier_schedule_collar(
        gmsh,
        endpoint=collar_endpoint,
        search=search,
        search_quality=search_quality,
        roots=roots,
        root_coordinates=root_coordinates,
        chains=chains,
        target_schedules=target_schedules,
        selected_directions=selected_directions,
        source_triangles=source_triangles,
        source_triangle_wall_fingerprints=(
            source_triangle_wall_fingerprints
        ),
        source_triangle_normals=source_triangle_normals,
        incident_normals=incident_normals,
        chain_origin=chain_origin,
        subdivided_prisms=subdivided_prisms,
        core_volume_records=core_volume_records,
        prism_volume_fingerprints=prism_volume_fingerprints,
        prism_element_tags=prism_tags,
        core_element_tags=core_tags,
        core_tetra_element_tags=core_tetra_tags,
        minimum_prism_scaled_jacobian=prism_threshold,
        minimum_core_tetra_gamma=core_threshold,
        minimum_cone_margin=minimum_cone_margin,
        minimum_changed_direction_margin=(
            minimum_changed_direction_margin
        ),
        apply_state=apply_state,
        non_target_nodes=non_target_nodes,
    )
    collar_evaluation_count = int(
        collar_refinement_selection["evaluation_evidence"][
            "collar_quality_evaluation_count"
        ]
    )
    coupled_evaluation_count = int(
        collar_refinement_selection["evaluation_evidence"].get(
            "coupled_quality_evaluation_count", 0
        )
    )
    total_evaluation_count = (
        int(search["quality_evaluation_count"])
        + collar_evaluation_count
        + coupled_evaluation_count
    )
    if total_evaluation_count > int(caps["maximum_quality_evaluations"]):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier total quality cap was exceeded"
        )

    state_a1 = apply_state(previous_schedules, selected_directions)
    quality_a1 = full_quality()
    state_b1 = apply_state(selected_target_schedules, selected_directions)
    quality_b1 = full_quality()
    state_a2 = apply_state(previous_schedules, selected_directions)
    quality_a2 = full_quality()
    state_b2 = apply_state(selected_target_schedules, selected_directions)
    quality_b2 = full_quality()
    state_a_reproducible = (
        state_a1["coordinate_sha256"] == state_a2["coordinate_sha256"]
        and quality_a1["quality_sha256"] == quality_a2["quality_sha256"]
    )
    state_b_reproducible = (
        state_b1["coordinate_sha256"] == state_b2["coordinate_sha256"]
        and quality_b1["quality_sha256"] == quality_b2["quality_sha256"]
    )
    if (
        not state_a_reproducible
        or not state_b_reproducible
        or quality_b2["quality_sha256"]
        != collar_final_quality["quality_sha256"]
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier cross-schedule replay differs"
        )
    after_all_coordinates = _nodes_from_gmsh(gmsh)
    non_target_after_sha256 = _frontier_collar_node_set_sha256(
        after_all_coordinates, non_target_nodes
    )
    non_target_changed_count = sum(
        before_all_coordinates[node] != after_all_coordinates[node]
        for node in non_target_nodes
    )
    base_root_moved_count = sum(
        tuple(after_all_coordinates[root])
        != tuple(float(value) for value in root_coordinates[root])
        for root in roots
    )
    if (
        non_target_changed_count != 0
        or non_target_before_sha256 != non_target_after_sha256
        or base_root_moved_count != 0
    ):
        raise BoundaryLayerSmokeError(
            "physical-schedule frontier changed an unauthorized coordinate"
        )
    both_states_pass = (
        quality_a1["status"] == "PASS"
        and quality_a2["status"] == "PASS"
        and quality_b1["status"] == "PASS"
        and quality_b2["status"] == "PASS"
    )
    final_status = "PASS" if both_states_pass else "INCOMPLETE"
    observed_counts = {
        "projected_3d_element_count": len(prism_tags) + len(core_tags),
        "prism_element_count": len(prism_tags),
        "core_element_count": len(core_tags),
        "core_tetra_count": len(core_tetra_tags),
        "target_low_quality_prism_count": len(target_low_records),
        "target_unique_source_triangle_count": len(target_bad_triangles),
        "target_wall_count": target_observation["wall_count"],
        "component_count": len(runtime_components),
        "candidate_root_count": len(candidate_roots),
        "pattern_quality_evaluation_count": int(
            search["quality_evaluation_count"]
        ),
        "collar_quality_evaluation_count": collar_evaluation_count,
        "coupled_quality_evaluation_count": coupled_evaluation_count,
        "quality_evaluation_count": total_evaluation_count,
        "changed_root_count": len(selected_changed_records),
        "final_nonpositive_element_count": int(
            quality_b2["nonpositive_element_count"]
        ),
        "final_prism_below_threshold_count": int(
            quality_b2["prism_below_threshold_element_count"]
        ),
        "final_core_tetra_below_gamma_count": int(
            quality_b2["core_tetra_below_gamma_count"]
        ),
        "final_nonfinite_count": int(quality_b2["nonfinite_count"]),
    }
    unsigned: dict[str, Any] = {
        "schema": SCHEDULE_DIRECTION_CONTINUATION_DISCOVERY_SCHEMA,
        "status": final_status,
        "profile_complete": final_status == "PASS",
        "request": str(continuation_endpoint["request"]),
        "audit_only": True,
        "audit_variant": (
            "physical_schedule_first_frontier_direction_continuation"
        ),
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_write_authorized": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "runtime_mesh_tags_hardcoded": False,
        "source_bindings": dict(continuation_endpoint["source_bindings"]),
        "endpoint": dict(continuation_endpoint),
        "fixed_direction_input": {
            "approval_sha256": str(approval["approval_sha256"]),
            "incoming_direction_field_sha256": fixed[
                "direction_field_sha256"
            ],
            "candidate_root_count": fixed["candidate_root_count"],
            "changed_root_count": fixed["changed_root_count"],
            "changed_roots": fixed["changed_roots"],
            "root_context_replay": fixed["root_context_replay"],
            "interaction_topology_sha256": fixed[
                "interaction_topology_sha256"
            ],
            "historical_interaction_topology_sha256": fixed.get(
                "historical_interaction_topology_sha256",
                fixed["interaction_topology_sha256"],
            ),
            "portable_interaction_topology_rebuilt": fixed.get(
                "portable_interaction_topology_rebuilt", False
            ),
        },
        "frontier_input": {
            "previous_state": {
                "fraction_float_hex": previous_fraction.hex(),
                **previous_schedule_evidence,
                **previous_application,
                "quality": previous_quality,
            },
            "target_state": {
                "fraction_float_hex": target_fraction.hex(),
                **target_schedule_evidence,
                **target_application,
                "schedule_sha256": target_application[
                    "root_schedule_sha256"
                ],
                "quality": target_quality,
                "low_quality_records": target_low_records,
                "low_quality_aggregate": target_low_aggregate,
            },
        },
        "components": {
            "connected_by_shared_root": True,
            "split_by_wall_fingerprint": False,
            "component_count": len(runtime_components),
            "candidate_root_count": len(candidate_roots),
            "maximum_component_count": precheck[
                "maximum_component_count"
            ],
            "maximum_candidate_root_count": precheck[
                "maximum_candidate_root_count"
            ],
            "within_caps": precheck["within_caps"],
            "records": stable_components,
        },
        "pattern_search": search,
        "fine_refinement_selection": fine_refinement_selection,
        "triple_refinement_selection": triple_refinement_selection,
        "collar_refinement_selection": collar_refinement_selection,
        "selected_direction_field": {
            "incoming_direction_field_sha256": fixed[
                "direction_field_sha256"
            ],
            "selected_direction_field_sha256": selected_field_sha256,
            "changed_root_count": len(selected_changed_records),
            "changed_roots": selected_changed_records,
        },
        "cross_schedule_replay": {
            "sequence_fraction_float_hex": [
                previous_fraction.hex(),
                target_fraction.hex(),
                previous_fraction.hex(),
                target_fraction.hex(),
            ],
            "state_a_first_coordinate_sha256": state_a1[
                "coordinate_sha256"
            ],
            "state_b_first_coordinate_sha256": state_b1[
                "coordinate_sha256"
            ],
            "state_a_second_coordinate_sha256": state_a2[
                "coordinate_sha256"
            ],
            "state_b_second_coordinate_sha256": state_b2[
                "coordinate_sha256"
            ],
            "state_a_first_quality_sha256": quality_a1["quality_sha256"],
            "state_b_first_quality_sha256": quality_b1["quality_sha256"],
            "state_a_second_quality_sha256": quality_a2["quality_sha256"],
            "state_b_second_quality_sha256": quality_b2["quality_sha256"],
            "state_a_reproducible": state_a_reproducible,
            "state_b_reproducible": state_b_reproducible,
            "both_states_pass": both_states_pass,
            "base_root_moved_count": base_root_moved_count,
            "non_target_node_changed_count": non_target_changed_count,
            "non_target_coordinate_sha256": non_target_after_sha256,
            "selected_schedule_sha256": state_b2[
                "root_schedule_sha256"
            ],
            "selected_coordinate_sha256": state_b2[
                "coordinate_sha256"
            ],
            "selected_quality_sha256": quality_b2["quality_sha256"],
        },
        "final_quality": quality_b2,
        "observed_counts": observed_counts,
        "incomplete_reason": (
            None
            if final_status == "PASS"
            else (
                "FIRST_FRONTIER_SCHEDULE_COLLAR_EXHAUSTED"
                if collar_refinement_selection["status"] == "EXHAUSTED"
                else "FIRST_FRONTIER_DIRECTION_SEARCH_EXHAUSTED"
            )
        ),
    }
    return {**unsigned, "discovery_sha256": _canonical_hash(unsigned)}


def _apply_one_layer_orientation_cone_subdivision(
    gmsh: Any,
    *,
    prism_volume_tags: Sequence[int],
    core_volume_tags: Sequence[int],
    wall_surface_tags: Sequence[int],
    top_surface_tags: Sequence[int],
    lateral_surface_tags: Sequence[int],
    prism_volume_fingerprints: Mapping[int, str],
    normalized_config: Mapping[str, Any],
    audit_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair the generated one-layer closure and subdivide it to 15 layers.

    Selection is entirely dynamic: signed-volume failures select orientation
    reversals, node-local Jacobians select Chebyshev roots, and failed full
    layer prisms select the small direction-smoothing neighbourhood.  Runtime
    tags are retained only as audit evidence and are never used as constants.
    """

    repair = normalized_config.get("post_mesh_repair")
    if not isinstance(repair, Mapping) or repair.get("mode") != (
        "one_layer_orientation_cone_subdivision_v1"
    ):
        raise BoundaryLayerSmokeError("approved post-mesh repair mode is not configured")
    if audit_only and (
        normalized_config.get("repair_audit_only") is not True
        or normalized_config.get("contract_mode") != "coarse_repair_audit_only"
        or normalized_config.get("production_mesh_eligible") is not False
        or normalized_config.get("calibration_PASS_authorized") is not False
        or normalized_config.get("mesh_written") is not False
        or normalized_config.get("su2_called") is not False
        or normalized_config.get("paraview_called") is not False
    ):
        raise BoundaryLayerSmokeError(
            "repair discovery requires the explicit audit-only safety scope"
        )
    surface_schedules, schedule_binding_evidence = (
        _validated_local_surface_schedules(normalized_config)
    )
    layer_count = len(next(iter(surface_schedules.values())))
    final_first_height = next(iter(surface_schedules.values()))[0]
    construction_first_height = _finite(
        normalized_config.get("construction_first_layer_height_m"),
        "construction first-layer height",
    )
    if construction_first_height < final_first_height:
        raise BoundaryLayerSmokeError(
            "construction first-layer height is below the final physical height"
        )
    tolerance = float(repair["height_tolerance_m"])
    all_volume_entities = [
        *[int(value) for value in core_volume_tags],
        *[int(value) for value in prism_volume_tags],
    ]
    normalized_prism_fingerprints = {
        int(entity): str(fingerprint)
        for entity, fingerprint in prism_volume_fingerprints.items()
    }
    if set(normalized_prism_fingerprints) != {
        int(value) for value in prism_volume_tags
    } or any(not value for value in normalized_prism_fingerprints.values()):
        raise BoundaryLayerSmokeError(
            "prism-volume stable fingerprint mapping is incomplete"
        )

    initial_raw = [
        record
        for entity in prism_volume_tags
        for record in _element_records_from_gmsh(gmsh, 3, int(entity))
    ]
    initial_core_records = [
        record
        for record in _element_records_from_gmsh(gmsh, 3, -1)
        if record["type"] != "Prism 6"
    ]
    if not initial_raw or any(record["type"] != "Prism 6" for record in initial_raw):
        raise BoundaryLayerSmokeError("generated boundary layer is not one-layer Prism6")
    if not initial_core_records or any(
        record["type"] == "Prism 6" for record in initial_core_records
    ):
        raise BoundaryLayerSmokeError(
            "generated core inventory is empty or contains Prism6"
        )
    source_prism_tags = [int(record["tag"]) for record in initial_raw]
    initial_quality, initial_bad = _strict_quality_summary(gmsh, source_prism_tags)
    if not audit_only and len(initial_bad) != int(
        repair["expected_initial_bad_prism_count"]
    ):
        raise BoundaryLayerSmokeError(
            "generated one-layer bad Prism6 count differs from the repair contract: "
            f"observed={len(initial_bad)}, "
            f"expected={int(repair['expected_initial_bad_prism_count'])}, "
            "metrics="
            + repr(initial_quality["metrics"])
        )
    signed_volumes = [
        float(value)
        for value in gmsh.model.mesh.getElementQualities(source_prism_tags, "volume")
    ]
    if len(signed_volumes) != len(source_prism_tags) or any(
        not math.isfinite(value) for value in signed_volumes
    ):
        raise BoundaryLayerSmokeError("one-layer signed volumes are incomplete or non-finite")
    reversed_tags = [
        tag for tag, value in zip(source_prism_tags, signed_volumes) if value <= 0.0
    ]
    if not audit_only and len(reversed_tags) != int(
        repair["expected_negative_volume_prism_count"]
    ):
        raise BoundaryLayerSmokeError(
            "negative-volume Prism6 count differs from the repair contract: "
            f"observed={len(reversed_tags)}, "
            f"expected={int(repair['expected_negative_volume_prism_count'])}"
        )
    unordered_before = {
        int(record["tag"]): tuple(sorted(int(value) for value in record["nodes"]))
        for record in initial_raw
    }
    gmsh.model.mesh.reverseElements(reversed_tags)

    topology = _one_layer_prism_topology(
        gmsh,
        prism_volume_tags=prism_volume_tags,
        wall_surface_tags=wall_surface_tags,
        top_surface_tags=top_surface_tags,
        max_elements=int(normalized_config["max_3d_elements"]),
    )
    source_prisms = topology["records"]
    unordered_after = {
        int(record["tag"]): tuple(sorted(int(value) for value in record["nodes"]))
        for record in source_prisms
    }
    if unordered_before != unordered_after:
        raise BoundaryLayerSmokeError("Prism6 orientation repair changed element node sets")
    base_to_top = dict(topology["base_to_top"])
    inverse_top = dict(topology["inverse_top"])
    base_nodes = set(topology["base_nodes"])
    top_nodes = set(topology["top_nodes"])
    coordinates = _nodes_from_gmsh(gmsh)
    root_cumulative_heights, root_schedule_evidence = _assign_root_local_schedules(
        source_prisms,
        prism_volume_fingerprints=normalized_prism_fingerprints,
        surface_schedules=surface_schedules,
        root_coordinates=coordinates,
    )
    if set(root_cumulative_heights) != base_nodes:
        raise BoundaryLayerSmokeError(
            "not every one-layer prism root received a local physical schedule"
        )

    incident_normals: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    violating_roots: set[int] = set()
    prism_by_entity: dict[int, list[dict[str, Any]]] = defaultdict(list)
    source_triangles: set[tuple[int, int, int]] = set()
    source_triangle_normals: dict[
        tuple[int, int, int], tuple[float, float, float]
    ] = {}
    source_triangle_wall_fingerprints: dict[tuple[int, int, int], str] = {}
    local_coordinates_by_type: dict[int, list[float]] = {}
    for record in source_prisms:
        entity = int(record["entity"])
        prism_by_entity[entity].append(record)
        base_face = tuple(int(value) for value in record["base_face"])
        source_triangle = tuple(sorted(base_face))
        if source_triangle in source_triangles:
            raise BoundaryLayerSmokeError("one-layer source triangle has multiple prisms")
        source_triangles.add(source_triangle)
        first, second, third = (coordinates[node] for node in base_face)
        normal = _unit_vector(
            _vector_cross(
                _vector_subtract(second, first),
                _vector_subtract(third, first),
            )
        )
        if normal is None:
            raise BoundaryLayerSmokeError("one-layer source triangle is degenerate")
        mean_displacement = (0.0, 0.0, 0.0)
        for root in base_face:
            mean_displacement = _vector_add(
                mean_displacement,
                _vector_subtract(coordinates[base_to_top[root]], coordinates[root]),
            )
        if _vector_dot(normal, mean_displacement) < 0.0:
            normal = _vector_scale(normal, -1.0)
        if _vector_dot(normal, mean_displacement) <= 0.0:
            raise BoundaryLayerSmokeError("cannot orient a source triangle into its prism")
        source_triangle_normals[source_triangle] = normal
        source_triangle_wall_fingerprints[source_triangle] = (
            normalized_prism_fingerprints[entity]
        )
        for root in base_face:
            incident_normals[root].append(normal)

        element_type = int(record["element_type"])
        local_coordinates = local_coordinates_by_type.setdefault(
            element_type,
            [
                float(value)
                for value in gmsh.model.mesh.getElementProperties(element_type)[4]
            ],
        )
        determinants = [
            float(value)
            for value in gmsh.model.mesh.getJacobian(
                int(record["tag"]), local_coordinates
            )[1]
        ]
        if len(determinants) != len(record["nodes"]):
            raise BoundaryLayerSmokeError("node-local Prism6 Jacobians are incomplete")
        for index, node in enumerate(record["nodes"]):
            if node in base_nodes and (
                not math.isfinite(determinants[index]) or determinants[index] <= 0.0
            ):
                violating_roots.add(int(node))
    if not audit_only and len(violating_roots) != int(
        repair["expected_cone_node_count"]
    ):
        raise BoundaryLayerSmokeError(
            "node-local nonpositive Jacobian count differs from the repair contract: "
            f"observed={len(violating_roots)}, "
            f"expected={int(repair['expected_cone_node_count'])}"
        )

    original_coordinates = dict(coordinates)
    chebyshev_records: list[dict[str, Any]] = []
    chebyshev_directions: dict[int, tuple[float, float, float]] = {}
    for root in sorted(violating_roots):
        margin, direction, candidate_count = _chebyshev_cone_direction(
            incident_normals[root]
        )
        if margin < float(repair["minimum_cone_margin"]):
            raise BoundaryLayerSmokeError(
                "one-layer Chebyshev margin is below the configured gate"
            )
        top = int(base_to_top[root])
        target = _vector_add(
            coordinates[root], _vector_scale(direction, construction_first_height)
        )
        existing = gmsh.model.mesh.getNode(top)
        gmsh.model.mesh.setNode(top, list(target), list(existing[1]))
        coordinates[top] = target
        chebyshev_directions[root] = direction
        chebyshev_records.append(
            {
                "base_node_tag_audit": root,
                "top_node_tag_audit": top,
                "stable_root_coordinate_sha256": _stable_root_id(
                    coordinates[root]
                ),
                "incident_constraint_count": len(incident_normals[root]),
                "candidate_count": candidate_count,
                "max_min_unit_normal_margin": margin,
                "unit_direction": list(direction),
            }
        )

    after_one_layer_coordinates = _nodes_from_gmsh(gmsh)
    changed_coordinates = {
        tag
        for tag in original_coordinates
        if after_one_layer_coordinates[tag] != original_coordinates[tag]
    }
    expected_changed = {base_to_top[root] for root in violating_roots}
    if changed_coordinates != expected_changed or any(
        after_one_layer_coordinates[root] != original_coordinates[root]
        for root in base_nodes
    ):
        raise BoundaryLayerSmokeError("one-layer cone repair changed unapproved coordinates")
    source_norms = {
        root: math.sqrt(
            _vector_dot(
                _vector_subtract(
                    after_one_layer_coordinates[top], after_one_layer_coordinates[root]
                ),
                _vector_subtract(
                    after_one_layer_coordinates[top], after_one_layer_coordinates[root]
                ),
            )
        )
        for root, top in base_to_top.items()
    }
    source_norm_outliers = [
        root
        for root, value in source_norms.items()
        if abs(value - construction_first_height) > tolerance
    ]
    if source_norm_outliers:
        raise BoundaryLayerSmokeError("one-layer base/top displacement violates its tolerance")
    one_layer_volume_records = _all_volume_records_for_entities(
        gmsh, all_volume_entities
    )
    one_layer_quality, one_layer_bad = _strict_quality_summary(
        gmsh, [int(record["tag"]) for record in one_layer_volume_records]
    )
    if one_layer_bad:
        raise BoundaryLayerSmokeError("one-layer orientation/cone repair is not all-positive")
    source_nonprism_type_counts = Counter(
        str(record["type"])
        for record in one_layer_volume_records
        if record["type"] != "Prism 6"
    )
    projected_3d_element_count = _gate_subdivision_element_cap(
        source_prism_count=len(source_prisms),
        nonprism_type_counts=source_nonprism_type_counts,
        layer_count=layer_count,
        max_3d_elements=int(normalized_config["max_3d_elements"]),
    )

    lateral_records: list[dict[str, Any]] = []
    for surface in lateral_surface_tags:
        records = _element_records_from_gmsh(gmsh, 2, int(surface))
        if any(record["type"] != "Quadrilateral 4" for record in records):
            raise BoundaryLayerSmokeError("lateral shell contains a non-Quad4 element")
        lateral_records.extend(records)
    vertical_line_records: list[dict[str, Any]] = []
    for _, curve in gmsh.model.getEntities(1):
        for record in _element_records_from_gmsh(gmsh, 1, int(curve)):
            if record["type"] != "Line 2" or len(record["nodes"]) != 2:
                continue
            first, second = record["nodes"]
            if first in base_to_top and base_to_top[first] == second:
                vertical_line_records.append({**record, "root": first})
            elif second in base_to_top and base_to_top[second] == first:
                vertical_line_records.append({**record, "root": second})

    assignments: dict[int, tuple[int, int]] = {}
    for record in source_prisms:
        for root in record["base_face"]:
            assignments.setdefault(int(root), (3, int(record["entity"])))
    for record in lateral_records:
        for node in record["nodes"]:
            root = node if node in base_to_top else inverse_top.get(node)
            if root is not None:
                assignments[int(root)] = (2, int(record["entity"]))
    for record in vertical_line_records:
        assignments[int(record["root"])] = (1, int(record["entity"]))
    if set(assignments) != base_nodes:
        raise BoundaryLayerSmokeError("not every prism root has a node classification entity")

    directions: dict[int, tuple[float, float, float]] = {}
    for root, top in base_to_top.items():
        direction = _unit_vector(
            _vector_subtract(
                after_one_layer_coordinates[top], after_one_layer_coordinates[root]
            )
        )
        if direction is None:
            raise BoundaryLayerSmokeError("one-layer prism root has zero displacement")
        directions[root] = direction
    next_node_tag = max(after_one_layer_coordinates) + 1
    all_element_tags = [
        int(record["tag"])
        for dimension in (1, 2, 3)
        for record in _element_records_from_gmsh(gmsh, dimension, -1)
    ]
    next_element_tag = max(all_element_tags) + 1
    chains: dict[int, list[int]] = {}
    new_nodes_by_entity: dict[tuple[int, int], dict[str, list[Any]]] = defaultdict(
        lambda: {"tags": [], "coordinates": []}
    )
    for root in sorted(base_to_top):
        chain = [root]
        origin = after_one_layer_coordinates[root]
        direction = directions[root]
        for layer in range(layer_count - 1):
            node = next_node_tag
            next_node_tag += 1
            chain.append(node)
            target = _vector_add(
                origin,
                _vector_scale(direction, root_cumulative_heights[root][layer]),
            )
            payload = new_nodes_by_entity[assignments[root]]
            payload["tags"].append(node)
            payload["coordinates"].extend(target)
        top = int(base_to_top[root])
        top_target = _vector_add(
            origin,
            _vector_scale(direction, root_cumulative_heights[root][-1]),
        )
        existing = gmsh.model.mesh.getNode(top)
        gmsh.model.mesh.setNode(top, list(top_target), list(existing[1]))
        chain.append(top)
        chains[root] = chain
    for (dimension, entity), payload in sorted(new_nodes_by_entity.items()):
        gmsh.model.mesh.addNodes(
            dimension, entity, payload["tags"], payload["coordinates"]
        )

    for entity, records in sorted(prism_by_entity.items()):
        type_ids = {int(record["element_type"]) for record in records}
        if len(type_ids) != 1:
            raise BoundaryLayerSmokeError("Prism6 runtime type differs across one volume")
        gmsh.model.mesh.removeElements(
            3, entity, [int(record["tag"]) for record in records]
        )
        new_tags: list[int] = []
        new_connectivity: list[int] = []
        for record in records:
            for layer in range(layer_count):
                tag = int(record["tag"]) if layer == 0 else next_element_tag
                if layer:
                    next_element_tag += 1
                new_tags.append(tag)
                for node in record["nodes"]:
                    if node in base_to_top:
                        new_connectivity.append(chains[node][layer])
                    elif node in inverse_top:
                        new_connectivity.append(chains[inverse_top[node]][layer + 1])
                    else:
                        raise BoundaryLayerSmokeError("Prism6 node has no source chain")
        gmsh.model.mesh.addElementsByType(
            entity, next(iter(type_ids)), new_tags, new_connectivity
        )

    current_core_records = [
        record
        for record in _element_records_from_gmsh(gmsh, 3, -1)
        if record["type"] != "Prism 6"
    ]
    current_core_tags = {int(record["tag"]) for record in current_core_records}
    retained_core_records = {
        int(record["tag"]): record for record in current_core_records
    }
    for record in initial_core_records:
        tag = int(record["tag"])
        if tag in retained_core_records and tuple(
            int(value) for value in retained_core_records[tag]["nodes"]
        ) != tuple(int(value) for value in record["nodes"]):
            raise BoundaryLayerSmokeError(
                "Prism6 subdivision changed retained core connectivity"
            )
    missing_core_records = [
        record
        for record in initial_core_records
        if int(record["tag"]) not in current_core_tags
    ]
    missing_core_groups: dict[tuple[int, int], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for record in missing_core_records:
        missing_core_groups[
            (int(record["entity"]), int(record["element_type"]))
        ].append(record)
    for (entity, element_type), records in sorted(missing_core_groups.items()):
        new_tags = list(range(next_element_tag, next_element_tag + len(records)))
        next_element_tag += len(records)
        gmsh.model.mesh.addElementsByType(
            entity,
            element_type,
            new_tags,
            [
                int(node)
                for record in records
                for node in record["nodes"]
            ],
        )
    restored_core_records = [
        record
        for record in _element_records_from_gmsh(gmsh, 3, -1)
        if record["type"] != "Prism 6"
    ]
    if len(restored_core_records) != len(initial_core_records):
        raise BoundaryLayerSmokeError(
            "Prism6 subdivision did not preserve the core element inventory"
        )

    lateral_by_entity: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in lateral_records:
        lateral_by_entity[int(record["entity"])].append(record)
    for entity, records in sorted(lateral_by_entity.items()):
        type_ids = {int(record["element_type"]) for record in records}
        if len(type_ids) != 1:
            raise BoundaryLayerSmokeError("Quad4 runtime type differs across one surface")
        gmsh.model.mesh.removeElements(
            2, entity, [int(record["tag"]) for record in records]
        )
        new_tags: list[int] = []
        new_connectivity: list[int] = []
        for record in records:
            for layer in range(layer_count):
                tag = int(record["tag"]) if layer == 0 else next_element_tag
                if layer:
                    next_element_tag += 1
                new_tags.append(tag)
                for node in record["nodes"]:
                    if node in base_to_top:
                        new_connectivity.append(chains[node][layer])
                    elif node in inverse_top:
                        new_connectivity.append(chains[inverse_top[node]][layer + 1])
                    else:
                        raise BoundaryLayerSmokeError("Quad4 node has no source chain")
        gmsh.model.mesh.addElementsByType(
            entity, next(iter(type_ids)), new_tags, new_connectivity
        )

    lines_by_entity: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in vertical_line_records:
        lines_by_entity[int(record["entity"])].append(record)
    for entity, records in sorted(lines_by_entity.items()):
        type_ids = {int(record["element_type"]) for record in records}
        if len(type_ids) != 1:
            raise BoundaryLayerSmokeError("Line2 runtime type differs across one curve")
        gmsh.model.mesh.removeElements(
            1, entity, [int(record["tag"]) for record in records]
        )
        new_tags: list[int] = []
        new_connectivity: list[int] = []
        for record in records:
            chain = chains[int(record["root"])]
            forward = record["nodes"][0] == int(record["root"])
            for layer in range(layer_count):
                tag = int(record["tag"]) if layer == 0 else next_element_tag
                if layer:
                    next_element_tag += 1
                pair = [chain[layer], chain[layer + 1]]
                if not forward:
                    pair.reverse()
                new_tags.append(tag)
                new_connectivity.extend(pair)
        gmsh.model.mesh.addElementsByType(
            entity, next(iter(type_ids)), new_tags, new_connectivity
        )

    subdivided_prisms = [
        record
        for entity in prism_volume_tags
        for record in _element_records_from_gmsh(gmsh, 3, int(entity))
        if record["type"] == "Prism 6"
    ]
    initial_full_quality, full_bad = _strict_quality_summary(
        gmsh, [int(record["tag"]) for record in subdivided_prisms]
    )
    quality_contract = normalized_config.get("quality_improvement")
    if not isinstance(quality_contract, Mapping):
        raise BoundaryLayerSmokeError("quality-improvement contract is missing")
    initial_prism_tags = [int(record["tag"]) for record in subdivided_prisms]
    initial_min_sj_values = [
        float(value)
        for value in gmsh.model.mesh.getElementQualities(initial_prism_tags, "minSJ")
    ]
    if len(initial_min_sj_values) != len(initial_prism_tags) or any(
        not math.isfinite(value) for value in initial_min_sj_values
    ):
        raise BoundaryLayerSmokeError(
            "initial full-layer prism minSJ values are incomplete or non-finite"
        )
    prism_threshold = float(quality_contract["minimum_prism_scaled_jacobian"])
    initial_below_threshold = {
        tag
        for tag, value in zip(initial_prism_tags, initial_min_sj_values)
        if value < prism_threshold
    }
    repair_candidate_tags = set(int(value) for value in full_bad) | (
        initial_below_threshold
    )
    chain_origin = {
        node: (root, layer)
        for root, chain in chains.items()
        for layer, node in enumerate(chain)
    }
    bad_source_triangles: set[tuple[int, int, int]] = set()
    strict_bad_source_triangles: set[tuple[int, int, int]] = set()
    source_triangle_fingerprints: dict[tuple[int, int, int], str] = {}
    bad_records: list[dict[str, Any]] = []
    records_by_tag = {int(record["tag"]): record for record in subdivided_prisms}
    initial_min_sj_by_tag = dict(zip(initial_prism_tags, initial_min_sj_values))
    strict_bad_tags = {int(value) for value in full_bad}
    for tag in sorted(repair_candidate_tags):
        record = records_by_tag[tag]
        mapped = [chain_origin.get(int(node)) for node in record["nodes"]]
        if any(value is None for value in mapped):
            raise BoundaryLayerSmokeError("bad full-layer prism is not chain-mappable")
        roots = sorted({int(value[0]) for value in mapped if value is not None})
        levels = Counter(int(value[1]) for value in mapped if value is not None)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError("bad full-layer prism has invalid source topology")
        triangle = tuple(roots)
        bad_source_triangles.add(triangle)
        fingerprint = normalized_prism_fingerprints[int(record["entity"])]
        previous_fingerprint = source_triangle_fingerprints.setdefault(
            triangle, fingerprint
        )
        if previous_fingerprint != fingerprint:
            raise BoundaryLayerSmokeError(
                "one source triangle spans multiple stable wall fingerprints"
            )
        if tag in strict_bad_tags:
            strict_bad_source_triangles.add(triangle)
        bad_records.append(
            {
                "prism_tag_audit": tag,
                "layer": min(levels) + 1,
                "min_sj": initial_min_sj_by_tag[tag],
                "strict_nonpositive_quality": tag in strict_bad_tags,
                "below_configured_min_sj": tag in initial_below_threshold,
                "source_triangle_sha256": _stable_triangle_id(roots, coordinates),
                "source_root_tags_audit": roots,
            }
        )
    preferred_roots: set[int] = set()
    strict_ambiguous_triangles: set[tuple[int, int, int]] = set()
    for triangle in sorted(strict_bad_source_triangles):
        owners = set(triangle) & violating_roots
        if len(owners) != 1:
            if not audit_only:
                raise BoundaryLayerSmokeError(
                    "strict-failure source triangle has no unique dynamic sharp root"
                )
            strict_ambiguous_triangles.add(triangle)
            continue
        preferred_roots.update(owners)
    triangle_stable_ids = {
        triangle: _stable_triangle_id(triangle, coordinates)
        for triangle in bad_source_triangles
    }
    raw_direction_endpoint = schedule_binding_evidence.get(
        "owner_free_direction_endpoint"
    )
    raw_direction_replay = schedule_binding_evidence.get(
        "owner_free_direction_replay_approval"
    )
    if raw_direction_replay is not None:
        raw_homotopy_endpoint = schedule_binding_evidence.get(
            "physical_schedule_homotopy_endpoint"
        )
        raw_continuation_endpoint = schedule_binding_evidence.get(
            "schedule_direction_continuation_endpoint"
        )
        if raw_continuation_endpoint is not None:
            if (
                not audit_only
                or raw_direction_endpoint is not None
                or raw_homotopy_endpoint is None
                or normalized_config.get("audit_purpose")
                != "physical_schedule_first_frontier_direction_continuation"
                or normalized_config.get("fixed_direction_replay_only") is not True
                or normalized_config.get("combined_endpoint_only") is not True
                or normalized_config.get(
                    "first_frontier_direction_continuation_only"
                )
                is not True
            ):
                raise BoundaryLayerSmokeError(
                    "physical-schedule first-frontier continuation is audit-only "
                    "and exclusive"
                )
            all_volume_records = _element_records_from_gmsh(gmsh, 3, -1)
            core_volume_records = [
                record
                for record in all_volume_records
                if record["type"] != "Prism 6"
            ]
            discovery = (
                _run_physical_schedule_first_frontier_direction_continuation(
                    gmsh,
                    approval=raw_direction_replay,
                    schedule_evidence=schedule_binding_evidence,
                    root_coordinates=coordinates,
                    runtime_directions=directions,
                    chains=chains,
                    minimum_growth_bad_source_triangles=sorted(
                        bad_source_triangles
                    ),
                    source_triangles=sorted(source_triangles),
                    source_triangle_wall_fingerprints=(
                        source_triangle_wall_fingerprints
                    ),
                    source_triangle_normals=source_triangle_normals,
                    incident_normals=incident_normals,
                    source_prisms=source_prisms,
                    chain_origin=chain_origin,
                    subdivided_prisms=subdivided_prisms,
                    core_volume_records=core_volume_records,
                    prism_volume_fingerprints=(
                        normalized_prism_fingerprints
                    ),
                    minimum_prism_scaled_jacobian=prism_threshold,
                    minimum_core_tetra_gamma=float(
                        quality_contract["minimum_core_tetra_gamma"]
                    ),
                    minimum_cone_margin=float(
                        repair["minimum_cone_margin"]
                    ),
                    minimum_changed_direction_margin=float(
                        repair["minimum_smoothing_margin"]
                    ),
                )
            )
            verification_plan = {
                "mode": str(repair["mode"]),
                "prism_volume_tags": [int(value) for value in prism_volume_tags],
                "core_volume_tags": [int(value) for value in core_volume_tags],
                "lateral_surface_tags": [int(value) for value in lateral_surface_tags],
                "chains": {int(root): list(chain) for root, chain in chains.items()},
                "source_triangles": sorted(source_triangles),
                "root_cumulative_heights_m": {
                    int(root): list(values)
                    for root, values in root_cumulative_heights.items()
                },
                "height_tolerance_m": tolerance,
                "curved_chain_schedule": True,
                "schedule_verified_by_continuation_endpoint": True,
                "source_prism_count": len(source_prisms),
                "source_nonprism_type_counts": dict(
                    sorted(source_nonprism_type_counts.items())
                ),
                "source_lateral_quad_count": len(lateral_records),
                "source_vertical_line_count": len(vertical_line_records),
                "max_3d_elements": int(normalized_config["max_3d_elements"]),
            }
            if discovery.get("status") == "PASS":
                verification_plan["connectivity_sha256"] = _mesh_connectivity_sha256(gmsh)
                _verify_one_layer_orientation_cone_subdivision(gmsh, verification_plan)
                return verification_plan, discovery
            return {}, discovery
        if raw_homotopy_endpoint is not None:
            if (
                not audit_only
                or raw_direction_endpoint is not None
                or normalized_config.get("audit_purpose")
                != "physical_local_schedule_fixed_direction_homotopy"
                or normalized_config.get("fixed_direction_replay_only") is not True
                or normalized_config.get("combined_endpoint_only") is not True
            ):
                raise BoundaryLayerSmokeError(
                    "physical-schedule fixed-direction homotopy is audit-only "
                    "and exclusive"
                )
            core_volume_records = _all_volume_records_for_entities(
                gmsh, [int(value) for value in core_volume_tags]
            )
            discovery = _run_physical_schedule_fixed_direction_homotopy(
                gmsh,
                approval=raw_direction_replay,
                schedule_evidence=schedule_binding_evidence,
                root_coordinates=coordinates,
                runtime_directions=directions,
                chains=chains,
                bad_source_triangles=sorted(bad_source_triangles),
                bad_source_triangle_wall_fingerprints=(
                    source_triangle_fingerprints
                ),
                source_triangles=sorted(source_triangles),
                source_triangle_wall_fingerprints=(
                    source_triangle_wall_fingerprints
                ),
                source_prisms=source_prisms,
                chain_origin=chain_origin,
                subdivided_prisms=subdivided_prisms,
                core_volume_records=core_volume_records,
                prism_volume_fingerprints=normalized_prism_fingerprints,
                minimum_prism_scaled_jacobian=prism_threshold,
                minimum_core_tetra_gamma=float(
                    quality_contract["minimum_core_tetra_gamma"]
                ),
            )
            return {}, discovery
        if (
            not audit_only
            or raw_direction_endpoint is not None
            or normalized_config.get("audit_purpose")
            != "physical_local_schedule_fixed_direction_replay"
            or normalized_config.get("fixed_direction_replay_only") is not True
        ):
            raise BoundaryLayerSmokeError(
                "physical-schedule fixed-direction replay is audit-only and exclusive"
            )
        core_volume_records = _all_volume_records_for_entities(
            gmsh, [int(value) for value in core_volume_tags]
        )
        discovery = _run_physical_schedule_fixed_direction_replay(
            gmsh,
            approval=raw_direction_replay,
            schedule_evidence=schedule_binding_evidence,
            root_coordinates=coordinates,
            runtime_directions=directions,
            chains=chains,
            root_cumulative_heights=root_cumulative_heights,
            bad_source_triangles=sorted(bad_source_triangles),
            bad_source_triangle_wall_fingerprints=(
                source_triangle_fingerprints
            ),
            source_triangles=sorted(source_triangles),
            source_triangle_wall_fingerprints=(
                source_triangle_wall_fingerprints
            ),
            chain_origin=chain_origin,
            subdivided_prisms=subdivided_prisms,
            core_volume_records=core_volume_records,
            prism_volume_fingerprints=normalized_prism_fingerprints,
            minimum_prism_scaled_jacobian=prism_threshold,
            minimum_core_tetra_gamma=float(
                quality_contract["minimum_core_tetra_gamma"]
            ),
        )
        return {}, discovery
    if raw_direction_endpoint is not None:
        if not audit_only:
            raise BoundaryLayerSmokeError(
                "owner-free component direction search is audit-only"
            )
        try:
            direction_endpoint = validate_owner_free_direction_endpoint(
                raw_direction_endpoint,
                expected_schedule_endpoint_sha256=str(
                    schedule_binding_evidence[
                        "schedule_feasibility_endpoint"
                    ]["endpoint_sha256"]
                ),
            )
            runtime_components, stable_components = build_owner_free_components(
                sorted(bad_source_triangles),
                triangle_stable_ids=triangle_stable_ids,
                triangle_wall_fingerprints=source_triangle_fingerprints,
                root_coordinates=coordinates,
            )
        except (
            CoarseDirectionFeasibilityError,
            CoarseComponentDirectionError,
            KeyError,
        ) as error:
            raise BoundaryLayerSmokeError(
                f"owner-free component direction evidence is invalid: {error}"
            ) from error
        core_volume_records = _all_volume_records_for_entities(
            gmsh, [int(value) for value in core_volume_tags]
        )
        search, final_quality, selected_directions = (
            _run_owner_free_component_direction_search(
                gmsh,
                components=runtime_components,
                stable_components=stable_components,
                bad_source_triangles=sorted(bad_source_triangles),
                triangle_stable_ids=triangle_stable_ids,
                triangle_wall_fingerprints=source_triangle_fingerprints,
                prism_volume_fingerprints=normalized_prism_fingerprints,
                source_triangle_normals=source_triangle_normals,
                incident_normals=incident_normals,
                original_directions=directions,
                root_coordinates=coordinates,
                chains=chains,
                root_cumulative_heights=root_cumulative_heights,
                subdivided_prisms=subdivided_prisms,
                core_volume_records=core_volume_records,
                minimum_prism_scaled_jacobian=prism_threshold,
                minimum_core_tetra_gamma=float(
                    quality_contract["minimum_core_tetra_gamma"]
                ),
                minimum_cone_margin=float(
                    repair["minimum_cone_margin"]
                ),
                minimum_changed_direction_margin=float(
                    repair["minimum_smoothing_margin"]
                ),
                endpoint=direction_endpoint,
            )
        )
        stable_bad_triangles = sorted(
            [
                {
                    "source_triangle_sha256": triangle_stable_ids[triangle],
                    "wall_surface_fingerprint": source_triangle_fingerprints[
                        triangle
                    ],
                    "root_coordinate_sha256": sorted(
                        _stable_root_id(coordinates[root]) for root in triangle
                    ),
                }
                for triangle in bad_source_triangles
            ],
            key=lambda item: item["source_triangle_sha256"],
        )
        stable_observation = {
            "wall_surface_fingerprint_inventory": sorted(
                set(normalized_prism_fingerprints.values())
            ),
            "bad_source_triangles": stable_bad_triangles,
            "components": stable_components,
        }
        final_status = "PASS" if final_quality["status"] == "PASS" else "INCOMPLETE"
        observed_counts = {
            "initial_bad_prism_count": len(initial_bad),
            "negative_volume_prism_count": len(reversed_tags),
            "cone_node_count": len(violating_roots),
            "source_prism_count": len(source_prisms),
            "projected_3d_element_count": projected_3d_element_count,
            "initial_below_threshold_prism_count": len(
                initial_below_threshold
            ),
            "bad_source_triangle_count": len(bad_source_triangles),
            "component_count": len(runtime_components),
            "candidate_root_count": len(selected_directions),
            "quality_evaluation_count": int(search["quality_evaluation_count"]),
            "changed_root_count": len(search["changed_roots"]),
            "final_nonpositive_element_count": int(
                final_quality["nonpositive_element_count"]
            ),
            "final_prism_below_threshold_count": int(
                final_quality["prism_below_threshold_element_count"]
            ),
            "final_core_tetra_below_gamma_count": int(
                final_quality["core_tetra_below_gamma_count"]
            ),
            "final_nonfinite_count": int(final_quality["nonfinite_count"]),
        }
        verification_plan: dict[str, Any] = {
            "mode": str(repair["mode"]),
            "prism_volume_tags": [int(value) for value in prism_volume_tags],
            "core_volume_tags": [int(value) for value in core_volume_tags],
            "lateral_surface_tags": [int(value) for value in lateral_surface_tags],
            "chains": {int(root): list(chain) for root, chain in chains.items()},
            "source_triangles": sorted(source_triangles),
            "root_cumulative_heights_m": {
                int(root): list(values)
                for root, values in root_cumulative_heights.items()
            },
            "height_tolerance_m": tolerance,
            "source_prism_count": len(source_prisms),
            "source_nonprism_type_counts": dict(
                sorted(source_nonprism_type_counts.items())
            ),
            "source_lateral_quad_count": len(lateral_records),
            "source_vertical_line_count": len(vertical_line_records),
            "max_3d_elements": int(normalized_config["max_3d_elements"]),
        }
        if final_status == "PASS":
            verification_plan["connectivity_sha256"] = _mesh_connectivity_sha256(
                gmsh
            )
            _verify_one_layer_orientation_cone_subdivision(
                gmsh, verification_plan
            )
        discovery = {
            "schema": COMPONENT_DIRECTION_DISCOVERY_SCHEMA,
            "status": final_status,
            "profile_complete": final_status == "PASS",
            "audit_only": True,
            "audit_variant": "owner_free_component_direction",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "runtime_mesh_tags_hardcoded": False,
            "runtime_tags_are_audit_only": True,
            "repair_application_performed": True,
            "repair_application_scope": (
                "IN_MEMORY_OWNER_FREE_DIRECTION_COMPLETE_AUDIT_ONLY"
                if final_status == "PASS"
                else "IN_MEMORY_OWNER_FREE_DIRECTION_EXHAUSTED_AUDIT_ONLY"
            ),
            "in_memory_mesh_mutation_performed": True,
            "final_direction_application_performed": True,
            "incomplete_reason": (
                None
                if final_status == "PASS"
                else "OWNER_FREE_DIRECTION_SEARCH_EXHAUSTED"
            ),
            "observed_counts": observed_counts,
            "stable_observation": stable_observation,
            "stable_observation_sha256": _canonical_hash(stable_observation),
            "direction_endpoint": direction_endpoint,
            "search": search,
            "final_quality": final_quality,
            "orientation": {
                "initial_quality": initial_quality,
                "reversed_prism_count": len(reversed_tags),
                "unordered_cell_node_sets_unchanged": (
                    unordered_before == unordered_after
                ),
            },
            "one_layer_chebyshev": {
                "violating_root_count": len(violating_roots),
                "minimum_required_margin": float(repair["minimum_cone_margin"]),
                "postrepair_quality": one_layer_quality,
            },
            "full_layer_detection": {
                "initial_quality": initial_full_quality,
                "minimum_scaled_jacobian_for_repair": prism_threshold,
                "bad_prism_count": len(full_bad),
                "below_threshold_prism_count": len(initial_below_threshold),
                "bad_source_triangle_count": len(bad_source_triangles),
            },
            "subdivision": {
                "surface_schedule_binding": schedule_binding_evidence,
                "surface_schedule_binding_sha256": str(
                    schedule_binding_evidence["binding_sha256"]
                ),
                "schedule_endpoint_sha256": str(
                    direction_endpoint["schedule_endpoint_sha256"]
                ),
                "layer_count": layer_count,
                "root_count": len(chains),
                "source_prism_count": len(source_prisms),
                "projected_3d_element_count": projected_3d_element_count,
            },
        }
        return (
            verification_plan if final_status == "PASS" else {},
            discovery,
        )
    root_stable_ids = {
        root: _stable_root_id(coordinates[root]) for root in violating_roots
    }
    approved_ambiguous_owner_stable_ids = {
        (
            str(record["source_triangle_sha256"]),
            str(record["wall_surface_fingerprint"]),
        ): str(record["owner_root_coordinate_sha256"])
        for record in repair["approved_ambiguous_owners"]
    }
    approved_owner_evidence = {
        (
            str(record["source_triangle_sha256"]),
            str(record["wall_surface_fingerprint"]),
        ): dict(record)
        for record in repair["approved_ambiguous_owners"]
    }
    if audit_only and not bad_source_triangles:
        dynamic_groups = {}
        triangle_assignments = {}
        owner_ambiguities = []
    elif audit_only:
        strict_ambiguous_approval_keys = {
            (
                triangle_stable_ids[triangle],
                source_triangle_fingerprints[triangle],
            )
            for triangle in strict_ambiguous_triangles
        }
        (
            dynamic_groups,
            triangle_assignments,
            owner_ambiguities,
        ) = _discover_repair_source_triangle_groups(
            bad_source_triangles,
            violating_roots,
            preferred_roots=preferred_roots,
            forced_unresolved_triangles=strict_ambiguous_triangles,
            triangle_group_labels=source_triangle_fingerprints,
            triangle_stable_ids=triangle_stable_ids,
            root_stable_ids=root_stable_ids,
            approved_ambiguous_owner_stable_ids=(
                {
                    key: value
                    for key, value in approved_ambiguous_owner_stable_ids.items()
                    if key not in strict_ambiguous_approval_keys
                }
            ),
        )
    else:
        dynamic_groups, triangle_assignments = _group_repair_source_triangles(
            bad_source_triangles,
            violating_roots,
            preferred_roots=preferred_roots,
            triangle_group_labels=source_triangle_fingerprints,
            triangle_stable_ids=triangle_stable_ids,
            root_stable_ids=root_stable_ids,
            approved_ambiguous_owner_stable_ids=(
                approved_ambiguous_owner_stable_ids
            ),
        )
        owner_ambiguities = []
    triangle_evidence: list[dict[str, Any]] = []
    for triangle in sorted(bad_source_triangles):
        assignment = triangle_assignments[triangle]
        raw_sharp_root = assignment["sharp_root_tag_audit"]
        sharp_root = (
            int(raw_sharp_root) if raw_sharp_root is not None else None
        )
        # Report only roots that this repair is allowed to move.  Other sharp
        # roots on a multi-sharp triangle are deliberately protected.
        neighbours = (
            set(triangle) - {sharp_root} - violating_roots
            if sharp_root is not None
            else set()
        )
        stable_triangle_id = triangle_stable_ids[triangle]
        approval_key = (
            stable_triangle_id,
            source_triangle_fingerprints[triangle],
        )
        approval_record = approved_owner_evidence.get(approval_key)
        approval_consumed = assignment["assignment_method"] == (
            "matches_hash_bound_prior_strict_failure_owner"
        )
        triangle_evidence.append(
            {
                "source_triangle_sha256": stable_triangle_id,
                "source_root_tags_audit": list(triangle),
                "sharp_root_tag_audit": sharp_root,
                "owner_root_coordinate_sha256": (
                    root_stable_ids[sharp_root]
                    if sharp_root is not None
                    else None
                ),
                "neighbour_root_tags_audit": sorted(neighbours),
                "assignment_method": assignment["assignment_method"],
                "wall_surface_fingerprint": source_triangle_fingerprints[triangle],
                "approval_consumed": approval_consumed,
                "approval_evidence": approval_record if approval_consumed else None,
                "protected_other_sharp_root_tags_audit": assignment[
                    "protected_other_sharp_root_tags_audit"
                ],
            }
        )
    candidate_smoothed_root_count = sum(
        len(neighbours) for neighbours in dynamic_groups.values()
    )
    observed_contract = {
        "initial_full_layer_bad_prism_count": len(full_bad),
        "initial_below_threshold_prism_count": len(initial_below_threshold),
        "bad_source_triangle_count": len(bad_source_triangles),
        "smoothing_group_count": len(dynamic_groups),
        "candidate_smoothed_root_count": candidate_smoothed_root_count,
        "initial_quality": initial_full_quality,
        "strict_bad_source_triangle_count": len(strict_bad_source_triangles),
        "preferred_strict_failure_root_count": len(preferred_roots),
        "bad_records": bad_records,
        "triangle_evidence": triangle_evidence,
    }
    expected_contract = {
        "initial_full_layer_bad_prism_count": int(
            repair["expected_full_layer_bad_prism_count"]
        ),
        "initial_below_threshold_prism_count": int(
            repair["expected_initial_below_threshold_prism_count"]
        ),
        "bad_source_triangle_count": int(
            repair["expected_bad_source_triangle_count"]
        ),
        "smoothing_group_count": int(repair["expected_smoothing_group_count"]),
        "candidate_smoothed_root_count": int(
            repair["expected_smoothed_root_count"]
        ),
    }
    observed_counts = {
        key: observed_contract[key]
        for key in expected_contract
    }
    repair_contract_matches_observation = observed_counts == expected_contract
    complete_observed_counts = {
        "initial_bad_prism_count": len(initial_bad),
        "negative_volume_prism_count": len(reversed_tags),
        "cone_node_count": len(violating_roots),
        "full_layer_bad_prism_count": len(full_bad),
        "initial_below_threshold_prism_count": len(initial_below_threshold),
        "repair_candidate_prism_count": len(repair_candidate_tags),
        "bad_source_triangle_count": len(bad_source_triangles),
        "strict_bad_source_triangle_count": len(strict_bad_source_triangles),
        "preferred_strict_failure_root_count": len(preferred_roots),
        "smoothing_group_count": len(dynamic_groups),
        "candidate_smoothed_root_count": candidate_smoothed_root_count,
        "owner_ambiguity_count": len(owner_ambiguities),
        "changed_smoothed_root_count": 0,
        "source_prism_count": len(source_prisms),
        "projected_3d_element_count": projected_3d_element_count,
    }
    source_prism_by_tag = {
        int(record["tag"]): record for record in source_prisms
    }

    def stable_one_layer_prisms(tags: Iterable[int]) -> list[dict[str, Any]]:
        stable_records: list[dict[str, Any]] = []
        for tag in tags:
            record = source_prism_by_tag.get(int(tag))
            if record is None:
                raise BoundaryLayerSmokeError(
                    "one-layer repair tag is absent from dynamic prism topology"
                )
            roots = tuple(sorted(int(value) for value in record["base_face"]))
            stable_records.append(
                {
                    "source_triangle_sha256": _stable_triangle_id(
                        roots, coordinates
                    ),
                    "wall_surface_fingerprint": normalized_prism_fingerprints[
                        int(record["entity"])
                    ],
                }
            )
        return sorted(
            stable_records,
            key=lambda item: (
                item["wall_surface_fingerprint"],
                item["source_triangle_sha256"],
            ),
        )

    ambiguities_by_triangle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ambiguity in owner_ambiguities:
        ambiguities_by_triangle[str(ambiguity["source_triangle_sha256"])].append(
            ambiguity
        )
    stable_repair_sites: list[dict[str, Any]] = []
    for record in triangle_evidence:
        triangle_id = str(record["source_triangle_sha256"])
        stable_repair_sites.append(
            {
                "source_triangle_sha256": triangle_id,
                "wall_surface_fingerprint": str(
                    record["wall_surface_fingerprint"]
                ),
                "assignment_method": str(record["assignment_method"]),
                "owner_root_coordinate_sha256": record[
                    "owner_root_coordinate_sha256"
                ],
                "candidate_owner_root_coordinate_sha256": sorted(
                    {
                        str(value)
                        for ambiguity in ambiguities_by_triangle.get(
                            triangle_id, []
                        )
                        for value in ambiguity.get(
                            "candidate_root_coordinate_sha256", []
                        )
                    }
                ),
                "neighbour_root_coordinate_sha256": sorted(
                    {
                        _stable_root_id(coordinates[int(root)])
                        for root in record["neighbour_root_tags_audit"]
                    }
                ),
                "protected_root_coordinate_sha256": sorted(
                    {
                        _stable_root_id(coordinates[int(root)])
                        for root in record[
                            "protected_other_sharp_root_tags_audit"
                        ]
                    }
                ),
            }
        )
    stable_repair_sites.sort(
        key=lambda item: (
            item["wall_surface_fingerprint"],
            item["source_triangle_sha256"],
        )
    )
    stable_signature_payload = {
        "observed_counts": complete_observed_counts,
        "wall_surface_fingerprint_inventory": sorted(
            set(normalized_prism_fingerprints.values())
        ),
        "initial_bad_one_layer_prisms": stable_one_layer_prisms(initial_bad),
        "reversed_one_layer_prisms": stable_one_layer_prisms(reversed_tags),
        "repair_sites": stable_repair_sites,
    }
    stable_signature_sha256 = hashlib.sha256(
        json.dumps(
            stable_signature_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    if not audit_only and not repair_contract_matches_observation:
        raise BoundaryLayerSmokeError(
            "final physical layer schedule differs from the configured dynamic repair "
            f"contract: observed={observed_contract!r}, expected={expected_contract!r}"
        )
    if not audit_only and len(dynamic_groups) != int(
        repair["expected_smoothing_group_count"]
    ):
        raise BoundaryLayerSmokeError(
            "dynamic smoothing-group count differs from the repair contract: "
            f"observed={len(dynamic_groups)}, "
            f"expected={int(repair['expected_smoothing_group_count'])}"
        )
    if audit_only and owner_ambiguities:
        incomplete_evidence = {
            "schema": "cfdpipe.coarse_repair_discovery.v1",
            "status": "INCOMPLETE",
            "profile_complete": False,
            "audit_only": True,
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "runtime_mesh_tags_hardcoded": False,
            "runtime_tags_are_audit_only": True,
            "repair_application_performed": True,
            "repair_application_scope": "IN_MEMORY_PRE_SMOOTHING_AUDIT_ONLY",
            "in_memory_mesh_mutation_performed": True,
            "final_smoothing_application_performed": False,
            "incomplete_reason": "AMBIGUOUS_REPAIR_OWNER_REQUIRES_EXTERNAL_APPROVAL",
            "observed_counts": complete_observed_counts,
            "expected_counts": expected_contract,
            "repair_contract_matches_observation": (
                repair_contract_matches_observation
            ),
            "stable_signature": stable_signature_payload,
            "stable_signature_sha256": stable_signature_sha256,
            "owner_ambiguities": owner_ambiguities,
            "orientation": {
                "initial_quality": initial_quality,
                "reversed_prism_count": len(reversed_tags),
                "reversed_prism_tags_audit": sorted(reversed_tags),
                "unordered_cell_node_sets_unchanged": (
                    unordered_before == unordered_after
                ),
            },
            "one_layer_chebyshev": {
                "violating_root_count": len(violating_roots),
                "minimum_required_margin": float(repair["minimum_cone_margin"]),
                "records": chebyshev_records,
                "postrepair_quality": one_layer_quality,
            },
            "full_layer_detection": {
                "initial_quality": initial_full_quality,
                "minimum_scaled_jacobian_for_repair": prism_threshold,
                "bad_records": bad_records,
                "triangle_evidence": triangle_evidence,
            },
            "subdivision": {
                "surface_schedule_binding": schedule_binding_evidence,
                "root_schedule_assignment": root_schedule_evidence,
                "layer_count": layer_count,
                "root_count": len(chains),
                "projected_3d_element_count": projected_3d_element_count,
            },
        }
        return {}, incomplete_evidence
    neighbour_owner: dict[int, int] = {}
    smoothing_records: list[dict[str, Any]] = []
    for sharp_root in sorted(dynamic_groups):
        target = directions[sharp_root]
        for root in sorted(dynamic_groups[sharp_root]):
            previous = neighbour_owner.setdefault(root, sharp_root)
            if previous != sharp_root:
                raise BoundaryLayerSmokeError(
                    "one smoothing neighbour belongs to multiple sharp-root groups"
                )
            original = directions[root]
            repaired_direction, margin, alignment, candidate_count = (
                _closest_feasible_cone_direction(
                    target,
                    incident_normals[root],
                    original,
                    interior_weight=float(repair["smoothing_interior_weight"]),
                )
            )
            if margin < float(repair["minimum_smoothing_margin"]):
                raise BoundaryLayerSmokeError(
                    "smoothed direction margin is below the configured gate"
                )
            directions[root] = repaired_direction
            origin = coordinates[root]
            for layer, node in enumerate(chains[root][1:], start=1):
                target_coordinate = _vector_add(
                    origin,
                    _vector_scale(
                        repaired_direction,
                        root_cumulative_heights[root][layer - 1],
                    ),
                )
                existing = gmsh.model.mesh.getNode(node)
                gmsh.model.mesh.setNode(
                    node, list(target_coordinate), list(existing[1])
                )
            smoothing_records.append(
                {
                    "sharp_root_tag_audit": sharp_root,
                    "root_tag_audit": root,
                    "stable_root_coordinate_sha256": _stable_root_id(
                        coordinates[root]
                    ),
                    "candidate_count": candidate_count,
                    "incident_constraint_count": len(incident_normals[root]),
                    "minimum_incident_normal_dot": margin,
                    "alignment_with_sharp_direction": alignment,
                    "original_direction": list(original),
                    "repaired_direction": list(repaired_direction),
                }
            )
    if not audit_only and len(smoothing_records) != int(
        repair["expected_smoothed_root_count"]
    ):
        raise BoundaryLayerSmokeError(
            "smoothed-root count differs from the repair contract: "
            f"observed={len(smoothing_records)}, "
            f"expected={int(repair['expected_smoothed_root_count'])}"
        )
    if audit_only:
        complete_observed_counts["changed_smoothed_root_count"] = len(
            smoothing_records
        )
        stable_signature_sha256 = hashlib.sha256(
            json.dumps(
                stable_signature_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()

    verification_plan: dict[str, Any] = {
        "mode": str(repair["mode"]),
        "prism_volume_tags": [int(value) for value in prism_volume_tags],
        "core_volume_tags": [int(value) for value in core_volume_tags],
        "lateral_surface_tags": [int(value) for value in lateral_surface_tags],
        "chains": {int(root): list(chain) for root, chain in chains.items()},
        "source_triangles": sorted(source_triangles),
        "root_cumulative_heights_m": {
            int(root): list(values)
            for root, values in root_cumulative_heights.items()
        },
        "height_tolerance_m": tolerance,
        "source_prism_count": len(source_prisms),
        "source_nonprism_type_counts": dict(sorted(source_nonprism_type_counts.items())),
        "source_lateral_quad_count": len(lateral_records),
        "source_vertical_line_count": len(vertical_line_records),
        "max_3d_elements": int(normalized_config["max_3d_elements"]),
    }
    verification_plan["connectivity_sha256"] = _mesh_connectivity_sha256(gmsh)
    final_verification = _verify_one_layer_orientation_cone_subdivision(
        gmsh, verification_plan
    )
    evidence = {
        "schema": (
            "cfdpipe.coarse_repair_discovery.v1"
            if audit_only
            else "cfdpipe.one_layer_orientation_cone_subdivision.v1"
        ),
        "status": "PASS",
        **(
            {
                "profile_complete": True,
                "audit_only": True,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "runtime_tags_are_audit_only": True,
                "repair_application_performed": True,
                "repair_application_scope": "IN_MEMORY_COMPLETE_AUDIT_ONLY",
                "in_memory_mesh_mutation_performed": True,
                "final_smoothing_application_performed": True,
                "observed_counts": dict(complete_observed_counts),
                "expected_counts": expected_contract,
                "repair_contract_matches_observation": (
                    repair_contract_matches_observation
                ),
                "stable_signature": stable_signature_payload,
                "stable_signature_sha256": stable_signature_sha256,
                "owner_ambiguities": [],
            }
            if audit_only
            else {}
        ),
        "mode": str(repair["mode"]),
        "runtime_mesh_tags_hardcoded": False,
        "selection_method": (
            "signed volume -> node-local Jacobian -> complete Chebyshev cone -> "
            "full-layer failed source triangles -> complete incident-cone projection"
        ),
        "construction_scaffold": {
            "construction_first_layer_height_m": construction_first_height,
            "final_physical_first_layer_height_m": final_first_height,
            "layer_count": 1,
            "source_prism_count": len(source_prisms),
            "base_node_count": len(base_nodes),
            "top_node_count": len(top_nodes),
            "temporary_height_is_not_serialized_as_final_spacing": True,
            "construction_may_influence_core_topology_and_is_audited": True,
        },
        "orientation": {
            "initial_quality": initial_quality,
            "reversed_prism_count": len(reversed_tags),
            "reversed_prism_tags_audit": sorted(reversed_tags),
            "unordered_cell_node_sets_unchanged": unordered_before == unordered_after,
        },
        "one_layer_chebyshev": {
            "violating_root_count": len(violating_roots),
            "minimum_required_margin": float(repair["minimum_cone_margin"]),
            "records": chebyshev_records,
            "all_base_to_top_displacement_norms": {
                "count": len(source_norms),
                "minimum_m": min(source_norms.values()),
                "maximum_m": max(source_norms.values()),
                "target_m": construction_first_height,
                "tolerance_m": tolerance,
                "outlier_count": len(source_norm_outliers),
            },
            "postrepair_quality": one_layer_quality,
        },
        "full_layer_detection": {
            "initial_quality": initial_full_quality,
            "bad_prism_count": len(full_bad),
            "minimum_scaled_jacobian_for_repair": prism_threshold,
            "below_threshold_prism_count": len(initial_below_threshold),
            "repair_candidate_prism_count": len(repair_candidate_tags),
            "bad_source_triangle_count": len(bad_source_triangles),
            "bad_records": bad_records,
            "triangle_evidence": triangle_evidence,
        },
        "direction_smoothing": {
            "group_count": len(dynamic_groups),
            "sharp_root_tags_audit": sorted(dynamic_groups),
            "changed_root_count": len(smoothing_records),
            "interior_weight": float(repair["smoothing_interior_weight"]),
            "minimum_required_margin": float(repair["minimum_smoothing_margin"]),
            "records": smoothing_records,
        },
        "subdivision": {
            "surface_schedule_binding": schedule_binding_evidence,
            "root_schedule_assignment": root_schedule_evidence,
            "temporary_construction_first_layer_height_m": construction_first_height,
            "final_physical_first_layer_height_m": final_first_height,
            "all_final_chain_coordinates_use_physical_schedule": True,
            "construction_height_used_as_final_spacing": False,
            "layer_count": layer_count,
            "root_count": len(chains),
            "new_intermediate_node_count": (layer_count - 1) * len(chains),
            "source_prism_count": len(source_prisms),
            "projected_3d_element_count": projected_3d_element_count,
            "element_cap_checked_before_mutation": True,
            "source_lateral_quad_count": len(lateral_records),
            "source_vertical_line_count": len(vertical_line_records),
            "minimum_total_physical_thickness_m": root_schedule_evidence[
                "minimum_root_total_thickness_m"
            ],
            "maximum_total_physical_thickness_m": root_schedule_evidence[
                "maximum_root_total_thickness_m"
            ],
            "wall_base_nodes_moved": 0,
            "original_top_node_count_relocated": len(top_nodes),
        },
        "verification": final_verification,
    }
    return verification_plan, evidence


def _verify_one_layer_orientation_cone_subdivision(
    gmsh: Any, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Strictly verify serialized or in-memory repaired chains and topology."""

    chains = {
        int(root): [int(value) for value in chain]
        for root, chain in plan["chains"].items()
    }
    raw_root_schedules = plan.get("root_cumulative_heights_m")
    if not isinstance(raw_root_schedules, Mapping):
        raise BoundaryLayerSmokeError(
            "repair verification has no root-specific local schedules"
        )
    root_cumulative = {
        int(root): [
            _finite(value, "verification cumulative height") for value in values
        ]
        for root, values in raw_root_schedules.items()
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
    }
    if set(root_cumulative) != set(chains):
        raise BoundaryLayerSmokeError(
            "repair verification root schedule coverage is incomplete"
        )
    tolerance = float(plan["height_tolerance_m"])
    schedule_lengths = {len(values) for values in root_cumulative.values()}
    if len(schedule_lengths) != 1:
        raise BoundaryLayerSmokeError(
            "repair verification root schedules have different layer counts"
        )
    layer_count = next(iter(schedule_lengths))
    if layer_count < _REQUIRED_LAYER_COUNT or any(
        len(chain) != layer_count + 1 for chain in chains.values()
    ):
        raise BoundaryLayerSmokeError("repair verification chain length is invalid")
    if any(
        any(value <= 0.0 for value in values)
        or any(right <= left for left, right in zip(values, values[1:]))
        for values in root_cumulative.values()
    ):
        raise BoundaryLayerSmokeError(
            "repair verification root schedule is not strictly increasing"
        )
    flattened = [node for chain in chains.values() for node in chain]
    if len(flattened) != len(set(flattened)):
        raise BoundaryLayerSmokeError("repair verification chains share mesh nodes")
    coordinates = _nodes_from_gmsh(gmsh)
    if not set(flattened) <= set(coordinates):
        raise BoundaryLayerSmokeError("repair verification chain node is missing")
    maximum_schedule_error = 0.0
    schedule_outlier_count = 0
    if plan.get("schedule_verified_by_continuation_endpoint") is not True:
        for root, chain in chains.items():
            origin = coordinates[root]
            schedule = root_cumulative[root]
            for index, node in enumerate(chain[1:]):
                reference = (
                    coordinates[chain[index]]
                    if plan.get("curved_chain_schedule") is True
                    else origin
                )
                displacement = _vector_subtract(coordinates[node], reference)
                distance = math.sqrt(_vector_dot(displacement, displacement))
                expected = (
                    schedule[index] - (schedule[index - 1] if index else 0.0)
                    if plan.get("curved_chain_schedule") is True
                    else schedule[index]
                )
                error = abs(distance - expected)
                maximum_schedule_error = max(maximum_schedule_error, error)
                schedule_outlier_count += int(error > tolerance)
    if schedule_outlier_count:
        raise BoundaryLayerSmokeError("repaired chain does not follow the configured schedule")

    chain_origin = {
        node: (root, layer)
        for root, chain in chains.items()
        for layer, node in enumerate(chain)
    }
    prism_records = [
        record
        for entity in plan["prism_volume_tags"]
        for record in _element_records_from_gmsh(gmsh, 3, int(entity))
    ]
    if any(record["type"] != "Prism 6" for record in prism_records):
        raise BoundaryLayerSmokeError("repaired prism volume contains a non-Prism6 element")
    column_layers: Counter[tuple[tuple[int, int, int], int]] = Counter()
    for record in prism_records:
        mapped = [chain_origin.get(int(node)) for node in record["nodes"]]
        if any(value is None for value in mapped):
            raise BoundaryLayerSmokeError("repaired Prism6 is not chain-mappable")
        roots = tuple(sorted({int(value[0]) for value in mapped if value is not None}))
        levels = Counter(int(value[1]) for value in mapped if value is not None)
        if (
            len(roots) != 3
            or len(levels) != 2
            or sorted(levels.values()) != [3, 3]
            or max(levels) != min(levels) + 1
        ):
            raise BoundaryLayerSmokeError("repaired Prism6 spans an invalid chain layer")
        column_layers[(roots, min(levels))] += 1
    source_triangles = {
        tuple(int(value) for value in triangle)
        for triangle in plan["source_triangles"]
    }
    expected_column_layers = {
        (triangle, layer)
        for triangle in source_triangles
        for layer in range(layer_count)
    }
    if set(column_layers) != expected_column_layers or any(
        count != 1 for count in column_layers.values()
    ):
        raise BoundaryLayerSmokeError(
            "repaired Prism6 columns do not match the configured local layer count"
        )

    lateral_quad_count = 0
    for surface in plan["lateral_surface_tags"]:
        for record in _element_records_from_gmsh(gmsh, 2, int(surface)):
            if record["type"] != "Quadrilateral 4":
                raise BoundaryLayerSmokeError("repaired lateral shell is not all Quad4")
            mapped = [chain_origin.get(int(node)) for node in record["nodes"]]
            if any(value is None for value in mapped):
                raise BoundaryLayerSmokeError("repaired Quad4 is not chain-mappable")
            roots = {int(value[0]) for value in mapped if value is not None}
            levels = Counter(int(value[1]) for value in mapped if value is not None)
            if (
                len(roots) != 2
                or len(levels) != 2
                or sorted(levels.values()) != [2, 2]
                or max(levels) != min(levels) + 1
            ):
                raise BoundaryLayerSmokeError("repaired Quad4 spans an invalid chain layer")
            lateral_quad_count += 1
    if lateral_quad_count != int(plan["source_lateral_quad_count"]) * layer_count:
        raise BoundaryLayerSmokeError("repaired lateral Quad4 count is inconsistent")

    vertical_pairs = {
        frozenset((chain[layer], chain[layer + 1]))
        for chain in chains.values()
        for layer in range(layer_count)
    }
    vertical_line_count = 0
    for _, curve in gmsh.model.getEntities(1):
        for record in _element_records_from_gmsh(gmsh, 1, int(curve)):
            if record["type"] == "Line 2" and frozenset(record["nodes"]) in vertical_pairs:
                vertical_line_count += 1
    if vertical_line_count != int(plan["source_vertical_line_count"]) * layer_count:
        raise BoundaryLayerSmokeError("repaired vertical Line2 count is inconsistent")

    volume_records = _all_volume_records_for_entities(
        gmsh,
        [
            *[int(value) for value in plan["core_volume_tags"]],
            *[int(value) for value in plan["prism_volume_tags"]],
        ],
    )
    if len(volume_records) > int(plan["max_3d_elements"]):
        raise BoundaryLayerSmokeError("repaired mesh exceeds the configured element cap")
    quality, bad = _strict_quality_summary(
        gmsh, [int(record["tag"]) for record in volume_records]
    )
    if bad:
        raise BoundaryLayerSmokeError("repaired full-layer mesh has nonpositive quality")
    prism_count = sum(record["type"] == "Prism 6" for record in volume_records)
    nonprism_type_counts = Counter(
        str(record["type"])
        for record in volume_records
        if record["type"] != "Prism 6"
    )
    if (
        prism_count != int(plan["source_prism_count"]) * layer_count
        or dict(sorted(nonprism_type_counts.items()))
        != dict(plan["source_nonprism_type_counts"])
    ):
        raise BoundaryLayerSmokeError("repaired full-layer 3D element counts changed")
    connectivity_hash = _mesh_connectivity_sha256(gmsh)
    if connectivity_hash != str(plan["connectivity_sha256"]):
        raise BoundaryLayerSmokeError("fresh repaired-mesh connectivity hash changed")
    chain_payload = json.dumps(
        sorted((root, chain) for root, chain in chains.items()),
        separators=(",", ":"),
    ).encode("ascii")
    return {
        "status": "PASS",
        "chain_topology_rebuilt_from_elements_using_persisted_chain_contract": True,
        "quality_requeried": True,
        "ownership_verification_deferred_to_mixed_mesh_audit": True,
        "root_count": len(chains),
        "source_triangle_count": len(source_triangles),
        "layer_count": layer_count,
        "minimum_root_total_thickness_m": min(
            values[-1] for values in root_cumulative.values()
        ),
        "maximum_root_total_thickness_m": max(
            values[-1] for values in root_cumulative.values()
        ),
        "prism6_count": prism_count,
        "nonprism_type_counts": dict(sorted(nonprism_type_counts.items())),
        "total_3d_element_count": len(volume_records),
        "lateral_quad_count": lateral_quad_count,
        "vertical_line_count": vertical_line_count,
        "maximum_cumulative_schedule_error_m": maximum_schedule_error,
        "schedule_outlier_count": schedule_outlier_count,
        "chain_map_sha256": hashlib.sha256(chain_payload).hexdigest(),
        "connectivity_sha256": connectivity_hash,
        "quality": quality,
    }


class RealProjectBoundaryLayerStrategy:
    """Real shared-topology BREP strategy with explicit normal-repair dispatch.

    The implicit route is the verified core-closure construction.  Scalar,
    vector repair routes require an explicitly injected
    hook; there is deliberately no automatic fallback for known bad columns.
    """

    def __init__(
        self,
        marker_config: Mapping[str, Any],
        topology_smoke_config: Mapping[str, Any],
        *,
        marker_config_sha256: str | None = None,
        topology_smoke_config_sha256: str | None = None,
        repair_hooks: Mapping[str, Callable[..., Mapping[str, Any]]] | None = None,
    ) -> None:
        self.marker_config = marker_config
        self.topology_smoke_config = topology_smoke_config
        self.marker_config_sha256 = marker_config_sha256
        self.topology_smoke_config_sha256 = topology_smoke_config_sha256
        self.repair_hooks = dict(repair_hooks or {})
        unknown = set(self.repair_hooks) - set(_NORMAL_MODES)
        if unknown or "implicit" in self.repair_hooks:
            raise BoundaryLayerSmokeError("normal repair hook names are invalid")
        self._readback_plan: dict[str, Any] | None = None
        self._phase_evidence: dict[str, dict[str, Any]] = {}

    def evidence(self, phase: str) -> Mapping[str, Any]:
        return dict(self._phase_evidence.get(phase, {}))

    @staticmethod
    def _configure_production_volume_sizes(
        gmsh: Any, config: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Install a volume-only size callback without remeshing wall faces.

        The frozen Pilot repair is tied to the h=0.4 wall triangulation.  A
        production core may therefore be refined only for dimension three;
        returning the incoming size for curves and surfaces preserves every
        stable wall root consumed by the frozen direction/schedule evidence.
        """

        raw = config.get("production_volume_regions")
        if raw is None:
            return None
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise BoundaryLayerSmokeError(
                "production volume regions must be an ordered list"
            )
        regions: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping) or set(item) != {
                "name",
                "bounds_m",
                "size_m",
                "transition_width_m",
            }:
                raise BoundaryLayerSmokeError(
                    f"production volume region {index} is incomplete"
                )
            name = str(item.get("name", "")).strip()
            bounds = item.get("bounds_m")
            if (
                not name
                or not isinstance(bounds, Sequence)
                or isinstance(bounds, (str, bytes))
                or len(bounds) != 6
            ):
                raise BoundaryLayerSmokeError(
                    f"production volume region {index} bounds are invalid"
                )
            parsed = [
                _finite(value, f"production volume region {index} bound")
                for value in bounds
            ]
            if any(parsed[axis] >= parsed[axis + 3] for axis in range(3)):
                raise BoundaryLayerSmokeError(
                    f"production volume region {index} bounds are empty"
                )
            size = _finite(
                item.get("size_m"), f"production volume region {index} size"
            )
            if size <= 0.0:
                raise BoundaryLayerSmokeError(
                    f"production volume region {index} size is not positive"
                )
            transition = _finite(
                item.get("transition_width_m"),
                f"production volume region {index} transition width",
            )
            if transition <= 0.0:
                raise BoundaryLayerSmokeError(
                    f"production volume region {index} transition is not positive"
                )
            regions.append(
                {
                    "name": name,
                    "bounds_m": parsed,
                    "size_m": size,
                    "transition_width_m": transition,
                }
            )
        if not regions:
            raise BoundaryLayerSmokeError("production volume regions are empty")

        def volume_size(
            dimension: int,
            _tag: int,
            x: float,
            y: float,
            z: float,
            incoming_size: float,
        ) -> float:
            if int(dimension) != 3:
                return float(incoming_size)
            selected = float(incoming_size)
            for region in regions:
                xmin, ymin, zmin, xmax, ymax, zmax = region["bounds_m"]
                offsets = (
                    max(xmin - x, 0.0, x - xmax),
                    max(ymin - y, 0.0, y - ymax),
                    max(zmin - z, 0.0, z - zmax),
                )
                distance = math.sqrt(sum(value * value for value in offsets))
                width = float(region["transition_width_m"])
                if distance <= width:
                    fraction = min(1.0, distance / width)
                    local_size = float(region["size_m"]) + fraction * (
                        float(incoming_size) - float(region["size_m"])
                    )
                    selected = min(selected, local_size)
            return selected

        gmsh.model.mesh.setSizeCallback(volume_size)
        return {
            "schema": "cfdpipe.production_volume_regions.v1",
            "status": "CONFIGURED",
            "dimension_3_only": True,
            "wall_surface_triangulation_preserved": True,
            "regions": regions,
            "regions_sha256": _canonical_hash({"regions": regions}),
        }

    @staticmethod
    def _refine_production_tetrahedral_cores(
        gmsh: Any,
        *,
        core_volume_tags: Sequence[int],
        prism_volume_tags: Sequence[int],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Refill each frozen triangular core shell with HXT tetrahedra.

        The accepted Pilot evidence binds every prism and every triangle on the
        prism/core interface.  Recursive centroid splits preserve those faces,
        but progressively flatten the outer children and therefore cannot meet
        the frozen core-gamma gate at production counts.  A temporary discrete
        Gmsh model lets HXT fill the *interior* of the exact frozen triangle
        shell: no boundary node or triangle is regenerated.
        """

        region_evidence = RealProjectBoundaryLayerStrategy._configure_production_volume_sizes(
            gmsh, config
        )
        if region_evidence is None:
            raise BoundaryLayerSmokeError(
                "production tetrahedral refinement requires volume regions"
            )
        coordinates = _nodes_from_gmsh(gmsh)
        main_model = str(gmsh.model.getCurrent())
        if not main_model:
            raise BoundaryLayerSmokeError("production core remesh has no current model")
        initial_core_count = 0
        shells: dict[int, list[tuple[int, int, int]]] = {}
        shell_components: dict[int, list[list[tuple[int, int, int]]]] = {}
        shell_nodes: dict[int, set[int]] = {}
        core_boundaries: dict[int, list[int]] = {}
        surface_faces: dict[int, list[tuple[int, int, int]]] = {}
        for raw_core in core_volume_tags:
            core = int(raw_core)
            records = _element_records_from_gmsh(gmsh, 3, core)
            if not records or any(record["type"] != "Tetrahedron 4" for record in records):
                raise BoundaryLayerSmokeError(
                    "production core remesh requires all-Tet4 core entities"
                )
            initial_core_count += len(records)
            triangles: list[tuple[int, int, int]] = []
            boundary = gmsh.model.getBoundary(
                [(3, core)], combined=False, oriented=True, recursive=False
            )
            if not boundary or any(int(dim) != 2 for dim, _tag in boundary):
                raise BoundaryLayerSmokeError("production core shell is incomplete")
            core_boundaries[core] = [int(tag) for _dim, tag in boundary]
            for _dimension, signed_tag in boundary:
                surface_tag = abs(int(signed_tag))
                reverse = int(signed_tag) < 0
                surface_records = _element_records_from_gmsh(gmsh, 2, surface_tag)
                if not surface_records or any(
                    record["type"] != "Triangle 3" for record in surface_records
                ):
                    raise BoundaryLayerSmokeError(
                        "production core shell requires all-Triangle3 surfaces"
                    )
                surface_faces.setdefault(
                    surface_tag,
                    [tuple(int(value) for value in record["nodes"]) for record in surface_records],
                )
                for record in surface_records:
                    nodes = tuple(int(value) for value in record["nodes"])
                    triangles.append(
                        (nodes[0], nodes[2], nodes[1]) if reverse else nodes
                    )
            if len(triangles) != len(set(tuple(sorted(face)) for face in triangles)):
                raise BoundaryLayerSmokeError("production core shell has duplicate faces")
            shells[core] = triangles
            shell_nodes[core] = {node for face in triangles for node in face}
            edge_to_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
            for face_index, face in enumerate(triangles):
                for left, right in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
                    edge_to_faces[tuple(sorted((left, right)))].append(face_index)
            if any(len(owners) != 2 for owners in edge_to_faces.values()):
                raise BoundaryLayerSmokeError("production core shell is not closed")
            adjacency: dict[int, set[int]] = defaultdict(set)
            for owners in edge_to_faces.values():
                adjacency[owners[0]].add(owners[1])
                adjacency[owners[1]].add(owners[0])
            remaining = set(range(len(triangles)))
            components: list[list[tuple[int, int, int]]] = []
            while remaining:
                pending = [remaining.pop()]
                indices: list[int] = []
                while pending:
                    current = pending.pop()
                    indices.append(current)
                    new = adjacency[current] & remaining
                    remaining.difference_update(new)
                    pending.extend(new)
                components.append([triangles[index] for index in sorted(indices)])
            shell_components[core] = components
        prism_count = sum(
            len(_element_records_from_gmsh(gmsh, 3, int(tag)))
            for tag in prism_volume_tags
        )
        generated: dict[int, dict[str, Any]] = {}
        for core in sorted(shell_components):
            attempts: list[dict[str, Any]] = []
            for global_sign in (1, -1):
                gmsh.model.add(f"production_component_hxt_{core}_{global_sign}")
                try:
                    triangle_type = int(gmsh.model.mesh.getElementType("triangle", 1))
                    surfaces: list[int] = []
                    component_signs: list[int] = []
                    next_triangle = 1
                    for component in shell_components[core]:
                        surface = int(gmsh.model.addDiscreteEntity(2))
                        surfaces.append(surface)
                        nodes = sorted({node for face in component for node in face})
                        gmsh.model.mesh.addNodes(
                            2, surface, nodes,
                            [value for node in nodes for value in coordinates[node]],
                        )
                        tags = list(range(next_triangle, next_triangle + len(component)))
                        next_triangle += len(component)
                        gmsh.model.mesh.addElementsByType(
                            surface, triangle_type, tags,
                            [node for face in component for node in face],
                        )
                        signed_six_volume = 0.0
                        for n0, n1, n2 in component:
                            a, b, c = coordinates[n0], coordinates[n1], coordinates[n2]
                            signed_six_volume += _vector_dot(a, _vector_cross(b, c))
                        if signed_six_volume == 0.0:
                            raise BoundaryLayerSmokeError("core shell component has zero signed volume")
                        component_signs.append(1 if signed_six_volume > 0.0 else -1)
                    boundary = [
                        global_sign * sign * surface
                        for sign, surface in zip(component_signs, surfaces)
                    ]
                    volume = int(gmsh.model.addDiscreteEntity(3, boundary=boundary))
                    gmsh.option.setNumber("Mesh.Algorithm3D", 10)
                    RealProjectBoundaryLayerStrategy._configure_production_volume_sizes(gmsh, config)
                    gmsh.model.mesh.generate(3)
                    gmsh.model.mesh.removeSizeCallback()
                    records = _element_records_from_gmsh(gmsh, 3, volume)
                    all_tet4 = bool(records) and all(
                        record["type"] == "Tetrahedron 4" for record in records
                    )
                    attempts.append({
                        "global_boundary_sign": global_sign,
                        "component_signs": component_signs,
                        "volume_element_count": len(records),
                        "all_tetrahedron_4": all_tet4,
                    })
                    if all_tet4:
                        generated[core] = {
                            "records": [tuple(int(node) for node in record["nodes"]) for record in records],
                            "coordinates": _nodes_from_gmsh(gmsh),
                            "boundary_nodes": set(shell_nodes[core]),
                            "orientation": global_sign,
                            "orientation_attempts": attempts,
                        }
                finally:
                    gmsh.model.remove()
                    gmsh.model.setCurrent(main_model)
                if core in generated:
                    break
            if core not in generated:
                raise BoundaryLayerSmokeError("component HXT core output is not all-Tet4")

        gmsh.model.mesh.clear([(3, int(tag)) for tag in core_volume_tags])
        next_node = int(gmsh.model.mesh.getMaxNodeTag()) + 1
        next_element = int(gmsh.model.mesh.getMaxElementTag()) + 1
        tetra_element_type = int(gmsh.model.mesh.getElementType("tetrahedron", 1))
        final_core_count = 0
        core_audits: list[dict[str, Any]] = []
        for core in sorted(generated):
            data = generated[core]
            boundary_nodes = data["boundary_nodes"]
            temp_coordinates = data["coordinates"]
            interior = sorted(set(temp_coordinates) - boundary_nodes)
            mapping = {node: node for node in boundary_nodes}
            for node in interior:
                mapping[node] = next_node
                next_node += 1
            if interior:
                gmsh.model.mesh.addNodes(
                    3,
                    core,
                    [mapping[node] for node in interior],
                    [value for node in interior for value in temp_coordinates[node]],
                )
            records = [tuple(mapping[node] for node in tet) for tet in data["records"]]
            tags = list(range(next_element, next_element + len(records)))
            next_element += len(records)
            gmsh.model.mesh.addElementsByType(
                core,
                tetra_element_type,
                tags,
                [node for record in records for node in record],
            )
            final_core_count += len(records)
            core_audits.append(
                {
                    "core_volume_tag_audit": core,
                    "frozen_boundary_triangle_count": len(shells[core]),
                    "frozen_boundary_node_count": len(boundary_nodes),
                    "closed_shell_component_count": len(shell_components[core]),
                    "accepted_shell_orientation": int(data["orientation"]),
                    "orientation_attempts": list(data["orientation_attempts"]),
                    "generated_interior_node_count": len(interior),
                    "generated_tetrahedron_count": len(records),
                }
            )
        final_total = prism_count + final_core_count
        maximum = _positive_int(config.get("max_3d_elements"), "maximum 3D elements")
        if final_total > maximum:
            raise BoundaryLayerSmokeError("production HXT core exceeds the element cap")
        return {
            "schema": "cfdpipe.production_discrete_core_hxt.v1",
            "status": "PASS",
            "face_nodes_added": 0,
            "prism_core_interface_preserved": True,
            "initial_core_tetra_count": initial_core_count,
            "final_core_tetra_count": final_core_count,
            "prism_count": prism_count,
            "final_total_3d_element_count": final_total,
            "cores": core_audits,
            "volume_regions": region_evidence,
        }

    @staticmethod
    def _layer_schedule(config: Mapping[str, Any], sign: int) -> list[float]:
        height = _finite(config.get("first_layer_height_m"), "first layer height")
        growth = _finite(config.get("growth_ratio"), "growth ratio")
        layers = _positive_int(config.get("layer_count"), "layer count")
        if height <= 0.0 or growth <= 1.0 or sign not in {-1, 1}:
            raise BoundaryLayerSmokeError("boundary-layer schedule is invalid")
        cumulative: list[float] = []
        running = 0.0
        for index in range(layers):
            running += height * growth**index
            cumulative.append(sign * running)
        return cumulative

    def _extrude(
        self,
        gmsh: Any,
        wall_tags: list[int],
        wall_fingerprints: Mapping[int, str],
        inward_sign: int,
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        mode = str(config.get("normal_mode", ""))
        heights = self._layer_schedule(config, inward_sign)
        if mode == "implicit":
            repair = config.get("post_mesh_repair")
            if not isinstance(repair, Mapping) or repair.get("mode") != (
                "one_layer_orientation_cone_subdivision_v1"
            ):
                raise BoundaryLayerSmokeError(
                    "implicit extrusion requires the approved post-mesh repair contract"
                )
            # The core is first closed and meshed against a numerically robust,
            # explicitly configured temporary shell.  The repair stage derives
            # topology and directions from that shell, then relocates every
            # final chain node to the physical first-height/growth schedule.
            # The construction height therefore never survives in the solver
            # mesh and may not be smaller than the final physical first layer.
            construction_height = _finite(
                config.get("construction_first_layer_height_m"),
                "construction first-layer height",
            )
            if construction_height < float(config["first_layer_height_m"]):
                raise BoundaryLayerSmokeError(
                    "construction first-layer height is below the final physical height"
                )
            heights = [inward_sign * construction_height]
            dimtags = gmsh.model.geo.extrudeBoundaryLayer(
                [(2, tag) for tag in wall_tags],
                [1] * len(heights),
                heights,
                True,
            )
            return {
                "extruded_dimtags": dimtags,
                "collar_surface_tags": {},
                "initial_extrusion_mode": "implicit",
                "temporary_construction_first_layer_height_m": construction_height,
                "final_physical_first_layer_height_m": float(
                    config["first_layer_height_m"]
                ),
            }
        hook = self.repair_hooks.get(mode)
        if mode not in _NORMAL_MODES or hook is None:
            raise BoundaryLayerSmokeError(
                f"normal_mode {mode!r} has no approved geometry repair hook"
            )
        return hook(
            gmsh=gmsh,
            wall_tags=list(wall_tags),
            wall_fingerprints=dict(wall_fingerprints),
            num_elements=[1] * len(heights),
            cumulative_heights_m=heights,
            normalized_config=config,
        )

    @staticmethod
    def _parse_extrusion(
        result: Mapping[str, Any], wall_tags: Sequence[int]
    ) -> tuple[dict[int, int], dict[int, int], list[int], Mapping[str, Any]]:
        raw_dimtags = result.get("extruded_dimtags")
        if not isinstance(raw_dimtags, Sequence):
            raise BoundaryLayerSmokeError("normal repair returned no extruded entities")
        dimtags = [(int(value[0]), int(value[1])) for value in raw_dimtags]
        top_tags: list[int] = []
        prism_tags: list[int] = []
        for index, (dimension, tag) in enumerate(dimtags):
            if dimension == 3:
                if index == 0 or dimtags[index - 1][0] != 2:
                    raise BoundaryLayerSmokeError("extruded volume has no preceding top surface")
                top_tags.append(dimtags[index - 1][1])
                prism_tags.append(tag)
        if len(top_tags) != len(wall_tags) or len(prism_tags) != len(wall_tags):
            raise BoundaryLayerSmokeError("extrusion does not return one top/volume per wall")
        returned_surfaces = [tag for dimension, tag in dimtags if dimension == 2]
        lateral = sorted(set(returned_surfaces) - set(top_tags))
        collar = result.get("collar_surface_tags", {})
        if not isinstance(collar, Mapping):
            raise BoundaryLayerSmokeError("collar surface mapping is invalid")
        return dict(zip(wall_tags, top_tags)), dict(zip(wall_tags, prism_tags)), lateral, collar

    def _payload(
        self, gmsh: Any, plan: Mapping[str, Any], *, phase: str
    ) -> dict[str, Any]:
        volume_regions = {
            int(plan["core1_tag"]): "fluid_core_1",
            int(plan["core2_tag"]): "fluid_core_2",
            **{int(tag): "boundary_layer" for tag in plan["prism_volume_tags"]},
        }
        current_volumes = {int(tag) for _, tag in gmsh.model.getEntities(3)}
        if not set(volume_regions) <= current_volumes:
            raise BoundaryLayerSmokeError("cached volume tags are absent from the mesh file")
        physical = _physical_group_inventory(gmsh)
        expected_surface_groups = {
            (2, str(name)): sorted(int(tag) for tag in tags)
            for name, tags in plan["marker_surface_tags"].items()
        }
        fluid_key = (3, str(plan["fluid_physical_name"]))
        expected_physical = {
            **expected_surface_groups,
            fluid_key: sorted(volume_regions),
        }
        if physical != expected_physical:
            raise BoundaryLayerSmokeError("physical groups changed or are incomplete")
        elements = _volume_elements_from_gmsh(
            gmsh,
            volume_regions,
            max_elements=int(plan["max_3d_elements"]),
        )
        marker_faces = {
            str(name): _surface_faces_from_gmsh(gmsh, tags)
            for name, tags in plan["marker_surface_tags"].items()
        }
        shared_faces = _surface_faces_from_gmsh(gmsh, plan["shared_surface_tags"])
        wall_faces_by_fingerprint = {
            str(fingerprint): _surface_faces_from_gmsh(gmsh, tags)
            for fingerprint, tags in plan["wall_surface_tags_by_fingerprint"].items()
        }
        collar_surface_tags = plan.get("collar_surface_tags", {})
        collar_faces = {
            str(fingerprint): _surface_faces_from_gmsh(gmsh, tags)
            for fingerprint, tags in collar_surface_tags.items()
        }
        collar_base_surface_tags = plan.get("collar_base_surface_tags", {})
        collar_base_faces = {
            str(fingerprint): _surface_faces_from_gmsh(gmsh, tags)
            for fingerprint, tags in collar_base_surface_tags.items()
        }
        columns, prism_base_faces = _derive_prism_columns(
            elements, wall_faces_by_fingerprint, collar_base_faces
        )
        quality_contract = plan.get("quality_improvement")
        if not isinstance(quality_contract, Mapping):
            raise BoundaryLayerSmokeError("quality-improvement plan is missing")
        core_tetra_quality = _core_tetra_quality_by_region(
            gmsh,
            wall_adjacent_core_volume_tag=int(plan["core1_tag"]),
            other_core_volume_tag=int(plan["core2_tag"]),
            minimum_gamma=float(quality_contract["minimum_core_tetra_gamma"]),
        )
        prism_column_quality = _prism_column_quality_summary(
            gmsh,
            columns=columns,
            prism_base_faces=prism_base_faces,
            minimum_scaled_jacobian=float(
                quality_contract["minimum_prism_scaled_jacobian"]
            ),
        )
        if int(core_tetra_quality["below_gamma_count"]) > int(
            quality_contract["maximum_core_tetra_below_gamma_count"]
        ):
            raise BoundaryLayerSmokeError(
                "core tetra gamma hard gate was not satisfied: "
                f"observed={core_tetra_quality!r}"
            )
        if (
            int(prism_column_quality["below_threshold_element_count"])
            > int(
                quality_contract[
                    "maximum_prism_below_threshold_element_count"
                ]
            )
            or int(prism_column_quality["below_threshold_column_count"])
            > int(
                quality_contract[
                    "maximum_prism_below_threshold_column_count"
                ]
            )
        ):
            raise BoundaryLayerSmokeError(
                "prism column scaled-Jacobian hard gate was not satisfied: "
                f"observed={prism_column_quality!r}"
            )
        quality_gate = {
            "status": "PASS",
            "core_tetra": core_tetra_quality,
            "prism_columns": prism_column_quality,
            "scoped_optimizer_policy": quality_contract[
                "scoped_optimizer_policy"
            ],
            "scoped_optimizer_evidence": quality_contract[
                "scoped_optimizer_evidence"
            ],
        }
        prism_lateral_faces, transition_faces = _derive_internal_role_faces(elements)
        role_faces = {
            "wall_base": [
                face for faces in wall_faces_by_fingerprint.values() for face in faces
            ],
            "prism_core_top": _surface_faces_from_gmsh(gmsh, plan["top_surface_tags"]),
            "prism_prism_lateral": prism_lateral_faces,
            "transition": transition_faces,
        }
        element_tags = [int(element["tag"]) for element in elements]
        repair_verification = _verify_one_layer_orientation_cone_subdivision(
            gmsh, plan["post_mesh_repair_plan"]
        )
        if phase == "build":
            repair_evidence = {
                **dict(plan["post_mesh_repair_evidence"]),
                "verification": repair_verification,
            }
        else:
            repair_evidence = {
                "schema": "cfdpipe.one_layer_orientation_cone_subdivision.v1",
                "status": "PASS",
                "mode": plan["post_mesh_repair_plan"]["mode"],
                "runtime_mesh_tags_hardcoded": False,
                "verification": repair_verification,
            }
        self._phase_evidence[phase] = {
            "direction_audit": plan["direction_audit"],
            "wall_fingerprint_by_runtime_tag_audit": {
                str(tag): fingerprint
                for tag, fingerprint in plan["wall_fingerprint_by_tag"].items()
            },
            "original_volume_tags_audit": plan["original_volume_tags"],
            "rebuilt_core_volume_tags_audit": [plan["core1_tag"], plan["core2_tag"]],
            "shared_surface_tags_audit": plan["shared_surface_tags"],
            "top_surface_tags_audit": plan["top_surface_tags"],
            "lateral_surface_tags_audit": plan["lateral_surface_tags"],
            "transition_surface_tags_audit": plan.get("transition_surface_tags", []),
            "physical_groups": {
                f"{dimension}:{name}": tags
                for (dimension, name), tags in sorted(physical.items())
            },
            "local_refinement": plan["local_refinement"],
            "mesh_options": plan["mesh_options"],
            "prism_quality_generation": plan.get("prism_quality_generation"),
            "quality_improvement": quality_gate,
            "post_mesh_repair": repair_evidence,
            "readback_reconstructed_physical_groups": phase == "readback",
        }
        return {
            "nodes": _nodes_from_gmsh(gmsh),
            "elements": elements,
            "marker_faces": marker_faces,
            "shared_interface_faces": shared_faces,
            "quality": _quality_from_gmsh(gmsh, element_tags),
            "wall_columns": columns,
            "collar_faces": collar_faces,
            "wall_base_faces": wall_faces_by_fingerprint,
            "prism_base_faces": prism_base_faces,
            "collar_base_faces": collar_base_faces,
            "role_faces": role_faces,
        }

    def build(
        self,
        gmsh: Any,
        source_brep: Path,
        normalized_config: Mapping[str, Any],
        staging_directory: Path,
    ) -> Mapping[str, Any]:
        del staging_directory
        from .geometry_repair import _collect_model
        from .topology_smoke_mesh import (
            _configure_local_refinement,
            _normalize_contract,
            _resolve_entities,
        )

        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        provenance = normalized_config.get("provenance")
        if not isinstance(provenance, Mapping):
            raise BoundaryLayerSmokeError("normalized provenance is missing")
        supplied_hashes = {
            "markers_sha256": self.marker_config_sha256,
            "topology_smoke_sha256": self.topology_smoke_config_sha256,
        }
        for field, supplied in supplied_hashes.items():
            normalized = str(supplied).casefold() if isinstance(supplied, str) else ""
            if (
                _SHA256.fullmatch(normalized) is None
                or normalized != provenance.get(field)
            ):
                raise BoundaryLayerSmokeError(f"real strategy {field} is not hash-bound")
        topology_provenance = self.topology_smoke_config.get("provenance")
        if not isinstance(topology_provenance, Mapping) or (
            str(topology_provenance.get("markers_sha256", "")).casefold()
            != provenance["markers_sha256"]
            or str(topology_provenance.get("pipeline_brep_sha256", "")).casefold()
            != provenance["pipeline_brep_sha256"]
        ):
            raise BoundaryLayerSmokeError("topology-smoke provenance is stale")
        imported = gmsh.model.occ.importShapes(str(source_brep))
        gmsh.model.occ.synchronize()
        if not imported:
            raise BoundaryLayerSmokeError("BREP import returned no entities")
        catalog = _collect_model(gmsh, require_normals=False)
        resolved = _resolve_entities(catalog, _normalize_contract(self.marker_config))
        diagnostic = resolved["surface_diagnostic_index"]
        wall_name = str(normalized_config["wall_physical_name"])
        wall_fingerprint_by_tag = {
            int(tag): str(record["fingerprint_id"])
            for tag, record in diagnostic.items()
            if str(record.get("physical_name")) == wall_name
        }
        if set(wall_fingerprint_by_tag.values()) != set(
            normalized_config["wall_surface_fingerprints"]
        ):
            raise BoundaryLayerSmokeError("fresh rematch does not resolve all 48 walls")
        surfaces = {int(item["entity_tag"]): item for item in catalog["surfaces"]}
        adjacent = {
            int(surfaces[tag]["adjacent_volumes"][0])
            for tag in wall_fingerprint_by_tag
            if len(surfaces[tag].get("adjacent_volumes", [])) == 1
        }
        if len(adjacent) != 1:
            raise BoundaryLayerSmokeError("walls do not bound one common fluid core")
        core1_original = next(iter(adjacent))
        volumes = {int(item["entity_tag"]): item for item in catalog["volumes"]}
        other = sorted(set(volumes) - {core1_original})
        if len(other) != 1:
            raise BoundaryLayerSmokeError("real project must contain exactly two fluid volumes")
        core2_original = other[0]
        core1_oriented = _oriented_surfaces(volumes[core1_original])
        core2_oriented = _oriented_surfaces(volumes[core2_original])
        wall_set = set(wall_fingerprint_by_tag)
        core1_nonwall = [value for value in core1_oriented if abs(value) not in wall_set]
        shared_tags = sorted(
            tag for tag, record in surfaces.items() if len(record.get("adjacent_volumes", [])) == 2
        )
        if len(shared_tags) != 2:
            raise BoundaryLayerSmokeError("fresh geometry must retain exactly two shared surfaces")

        configured_probe_distance = float(normalized_config["direction_probe_distance_m"])
        matching = self.marker_config.get("matching")
        if not isinstance(matching, Mapping):
            raise BoundaryLayerSmokeError("marker matching tolerances are missing")
        coordinate_tolerance = _finite(
            matching.get("coordinate_absolute_tolerance_m"),
            "marker coordinate tolerance",
        )
        if coordinate_tolerance <= 0.0:
            raise BoundaryLayerSmokeError("marker coordinate tolerance must be positive")
        probe_distances = [
            min(configured_probe_distance, multiplier * coordinate_tolerance)
            for multiplier in (5.0, 50.0, 500.0)
        ]
        directions: dict[int, dict[str, Any]] = {}
        for tag in sorted(wall_set):
            fingerprint = wall_fingerprint_by_tag[tag]
            try:
                evidence = _audit_wall_inward_direction(
                    gmsh,
                    catalog,
                    tag,
                    probe_distances_m=probe_distances,
                )
            except BaseException as error:
                directions[tag] = {
                    "status": "FAIL",
                    "surface_tag_audit": tag,
                    "surface_fingerprint_id": fingerprint,
                    "configured_probe_distance_m": configured_probe_distance,
                    "probe_distance_candidates_m": probe_distances,
                    "error": f"{type(error).__name__}: {error}",
                }
                self._phase_evidence["build"] = {
                    "direction_audit": directions,
                    "wall_fingerprint_by_runtime_tag_audit": {
                        str(runtime_tag): runtime_fingerprint
                        for runtime_tag, runtime_fingerprint in wall_fingerprint_by_tag.items()
                    },
                }
                raise BoundaryLayerSmokeError(
                    "wall direction audit failed for "
                    f"{fingerprint} (runtime tag {tag}): {error}"
                ) from error
            directions[tag] = {
                **evidence,
                "status": "PASS",
                "surface_fingerprint_id": fingerprint,
                "configured_probe_distance_m": configured_probe_distance,
                "probe_basis": "markers matching coordinate tolerance multipliers",
            }
        signs = {int(value["inward_height_sign"]) for value in directions.values()}
        self._phase_evidence["build"] = {
            "direction_audit": directions,
            "wall_fingerprint_by_runtime_tag_audit": {
                str(tag): fingerprint
                for tag, fingerprint in wall_fingerprint_by_tag.items()
            },
        }
        if len(signs) != 1:
            raise BoundaryLayerSmokeError("wall inward directions cannot share one extrusion sign")

        quality_improvement = normalized_config.get("quality_improvement")
        if not isinstance(quality_improvement, Mapping):
            raise BoundaryLayerSmokeError("quality-improvement contract is missing")
        fixed_options = {
            "General.NumThreads": 1.0,
            "Geometry.ExtrudeReturnLateralEntities": 1.0,
            "Mesh.ElementOrder": 1.0,
            "Mesh.RecombineAll": 0.0,
            "Mesh.MeshSizeFromCurvature": 0.0,
            "Mesh.MeshSizeFromPoints": 0.0,
            "Mesh.MeshSizeExtendFromBoundary": 0.0,
            "Mesh.MeshSizeMax": float(normalized_config["characteristic_length_m"]),
            "Mesh.SaveAll": 1.0,
            "Mesh.Binary": 0.0,
            "Mesh.MshFileVersion": 4.1,
            "Mesh.Algorithm": 6.0,
            "Mesh.Algorithm3D": float(
                quality_improvement["core_tetra_meshing_algorithm"]
            ),
        }
        for name, value in fixed_options.items():
            gmsh.option.setNumber(name, value)
        refinement_config = self.topology_smoke_config.get("local_refinement")
        if not isinstance(refinement_config, Mapping):
            raise BoundaryLayerSmokeError("topology-smoke local refinement is missing")
        refinement_audit = _configure_quality_improvement_fields(
            gmsh,
            resolved,
            refinement_config,
            quality_improvement,
            characteristic_length=float(normalized_config["characteristic_length_m"]),
            configure_local_refinement=_configure_local_refinement,
        )

        gmsh.model.occ.remove(gmsh.model.getEntities(3), recursive=False)
        gmsh.model.occ.synchronize()
        if gmsh.model.getEntities(3):
            raise BoundaryLayerSmokeError("original fluid volumes were not removed")
        extrusion = self._extrude(
            gmsh,
            sorted(wall_set),
            wall_fingerprint_by_tag,
            next(iter(signs)),
            normalized_config,
        )
        top_by_wall, prism_by_wall, lateral_tags, collar = self._parse_extrusion(
            extrusion, sorted(wall_set)
        )
        gmsh.model.geo.synchronize()
        top_signed = [
            (1 if value > 0 else -1) * top_by_wall[abs(value)]
            for value in core1_oriented
            if abs(value) in top_by_wall
        ]
        additional_core1 = extrusion.get("core1_additional_signed_surfaces", [])
        if not isinstance(additional_core1, Sequence):
            raise BoundaryLayerSmokeError("repair hook core shell additions are invalid")
        outer_loop = gmsh.model.geo.addSurfaceLoop(core1_nonwall)
        inner_loop = gmsh.model.geo.addSurfaceLoop(top_signed + [int(v) for v in additional_core1])
        core1_tag = int(gmsh.model.geo.addVolume([outer_loop, inner_loop]))
        core2_loop = gmsh.model.geo.addSurfaceLoop(core2_oriented)
        core2_tag = int(gmsh.model.geo.addVolume([core2_loop]))
        gmsh.model.geo.synchronize()

        marker_surface_tags = {
            name: list(record["entity_tags"])
            for name, record in resolved["physical_groups"].items()
            if int(record["dimension"]) == 2
        }
        normalized_collar = {
            str(fingerprint): [int(tag) for tag in tags]
            for fingerprint, tags in collar.items()
        }
        raw_collar_base = extrusion.get("collar_base_surface_tags", {})
        raw_transition = extrusion.get("transition_surface_tags", [])
        if not isinstance(raw_collar_base, Mapping) or not isinstance(
            raw_transition, Sequence
        ):
            raise BoundaryLayerSmokeError("repair hook collar/transition evidence is invalid")
        normalized_collar_base = {
            str(fingerprint): [int(tag) for tag in tags]
            for fingerprint, tags in raw_collar_base.items()
        }
        transition_tags = [int(tag) for tag in raw_transition]
        collar_marker_tags = sorted(
            {tag for tags in normalized_collar.values() for tag in tags}
        )
        if collar_marker_tags:
            marker_surface_tags[wall_name] = sorted(
                set(marker_surface_tags[wall_name]) | set(collar_marker_tags)
            )
        gmsh.model.removePhysicalGroups()
        for name, tags in marker_surface_tags.items():
            physical = gmsh.model.addPhysicalGroup(2, tags)
            gmsh.model.setPhysicalName(2, physical, name)
        all_fluid_volumes = [core1_tag, core2_tag, *prism_by_wall.values()]
        fluid_name = str(self.marker_config.get("fluid", {}).get("physical_name", ""))
        if not fluid_name:
            raise BoundaryLayerSmokeError("marker config has no fluid physical name")
        fluid_group = gmsh.model.addPhysicalGroup(3, all_fluid_volumes)
        gmsh.model.setPhysicalName(3, fluid_group, fluid_name)
        gmsh.model.mesh.generate(3)
        production_volume_regions = None
        if normalized_config.get("projection_only") is True:
            if normalized_config.get("contract_mode") != "coarse_projection_only":
                raise BoundaryLayerSmokeError(
                    "projection-only strategy mode is not explicitly authorized"
                )
            # This is an intentional early return: the real-project geometry,
            # stable 48-wall rematch, inward-direction proof, one temporary
            # Prism6 layer and both cores have been built by the same strategy,
            # but none of the physical-layer subdivision or quality repair is
            # allowed to run.  The projection module performs count-only
            # evidence extraction and cannot grant a mesh/calibration PASS.
            from .coarse_mesh_projection import summarize_one_layer_projection

            projection = summarize_one_layer_projection(
                gmsh,
                wall_fingerprint_by_tag=wall_fingerprint_by_tag,
                prism_by_wall=prism_by_wall,
                core_volume_tags=[core1_tag, core2_tag],
                normalized_config=normalized_config,
            )
            self._phase_evidence["build"] = {
                **self._phase_evidence.get("build", {}),
                "projection_only": projection,
                "local_refinement": refinement_audit,
                "mesh_options": fixed_options,
                "full_layer_subdivision_performed": False,
                "formal_quality_conclusion": "NOT_EVALUATED_OR_AUTHORIZED",
            }
            return projection
        repair_audit_only = normalized_config.get("repair_audit_only") is True
        post_mesh_repair_plan, post_mesh_repair_evidence = (
            _apply_one_layer_orientation_cone_subdivision(
                gmsh,
                prism_volume_tags=sorted(prism_by_wall.values()),
                core_volume_tags=[core1_tag, core2_tag],
                wall_surface_tags=sorted(wall_set),
                top_surface_tags=sorted(top_by_wall.values()),
                lateral_surface_tags=lateral_tags,
                prism_volume_fingerprints={
                    int(prism_by_wall[wall_tag]): wall_fingerprint_by_tag[wall_tag]
                    for wall_tag in sorted(prism_by_wall)
                },
                normalized_config=normalized_config,
                audit_only=repair_audit_only,
            )
        )
        production_replay_after_audit = (
            normalized_config.get("production_replay_after_audit") is True
        )
        if production_replay_after_audit and not repair_audit_only:
            raise BoundaryLayerSmokeError(
                "production replay requires the strict repair-audit path"
            )
        if repair_audit_only:
            expected_audit_schema = _expected_repair_audit_schema(
                normalized_config
            )
            if (
                normalized_config.get("contract_mode")
                != "coarse_repair_audit_only"
                or post_mesh_repair_evidence.get("schema")
                != expected_audit_schema
                or post_mesh_repair_evidence.get("audit_only") is not True
                or post_mesh_repair_evidence.get(
                    "calibration_PASS_authorized"
                )
                is not False
                or post_mesh_repair_evidence.get("production_mesh_eligible")
                is not False
                or post_mesh_repair_evidence.get("mesh_written") is not False
            ):
                raise BoundaryLayerSmokeError(
                    "repair audit hook returned evidence outside its safety scope"
                )
            self._phase_evidence["build"] = {
                **self._phase_evidence.get("build", {}),
                "repair_audit_only": True,
                "local_refinement": refinement_audit,
                "mesh_options": fixed_options,
                "production_volume_regions": production_volume_regions,
                "post_mesh_repair": post_mesh_repair_evidence,
                "mesh_written": False,
                "formal_quality_conclusion": "NOT_AUTHORIZED",
            }
            if not production_replay_after_audit:
                return post_mesh_repair_evidence
            final_quality = post_mesh_repair_evidence.get("final_quality")
            if (
                post_mesh_repair_evidence.get("status") != "PASS"
                or not isinstance(final_quality, Mapping)
                or final_quality.get("status") != "PASS"
                or int(final_quality.get("prism_below_threshold_element_count", -1))
                != 0
                or int(final_quality.get("nonpositive_element_count", -1)) != 0
                or int(final_quality.get("nonfinite_count", -1)) != 0
            ):
                raise BoundaryLayerSmokeError(
                    "frozen repair replay did not finish at the strict PASS state"
                )
            # The Pilot evidence is tied to the original h=0.4 wall/prism
            # topology and is therefore replayed before production core
            # refinement.  Tet4 centroid splits add no face nodes, so the
            # repaired prism/core interface remains exactly conformal.
            region_evidence = self._configure_production_volume_sizes(
                gmsh, normalized_config
            )
            if region_evidence is None:
                raise BoundaryLayerSmokeError("production core sizes are missing")
            all_coordinates = _nodes_from_gmsh(gmsh)
            prism_snapshots: dict[int, dict[str, Any]] = {}
            core_boundary_surfaces = {
                abs(int(tag))
                for _dim, tag in gmsh.model.getBoundary(
                    [(3, core1_tag), (3, core2_tag)],
                    combined=False,
                    oriented=True,
                    recursive=False,
                )
            }
            prism_boundary_surfaces = {
                abs(int(surface))
                for volume in prism_by_wall.values()
                for _dim, surface in gmsh.model.getBoundary(
                    [(3, int(volume))],
                    combined=False,
                    oriented=True,
                    recursive=False,
                )
            }
            interface_surface_snapshots = {
                tag: records
                for tag in sorted(prism_boundary_surfaces & core_boundary_surfaces)
                if (records := _element_records_from_gmsh(gmsh, 2, tag))
            }
            surface_snapshots = {
                tag: records
                for tag in sorted(prism_boundary_surfaces - core_boundary_surfaces)
                if (records := _element_records_from_gmsh(gmsh, 2, tag))
            }
            core_boundary_curves = {
                abs(int(curve))
                for surface in core_boundary_surfaces
                for _dim, curve in gmsh.model.getBoundary(
                    [(2, surface)], combined=False, oriented=True, recursive=False
                )
            }
            prism_boundary_curves = {
                abs(int(curve))
                for surface in prism_boundary_surfaces
                for _dim, curve in gmsh.model.getBoundary(
                    [(2, surface)], combined=False, oriented=True, recursive=False
                )
            }
            curve_snapshots = {
                tag: records
                for tag in sorted(prism_boundary_curves - core_boundary_curves)
                if (records := _element_records_from_gmsh(gmsh, 1, tag))
            }
            for raw_tag in prism_by_wall.values():
                tag = int(raw_tag)
                records = _element_records_from_gmsh(gmsh, 3, tag)
                if not records or any(record["type"] != "Prism 6" for record in records):
                    raise BoundaryLayerSmokeError("production prism snapshot is incomplete")
                node_tags, _node_coordinates, _ = gmsh.model.mesh.getNodes(3, tag)
                classified = [int(value) for value in node_tags]
                prism_snapshots[tag] = {
                    "nodes": classified,
                    "records": records,
                }
            maximum_node_tag = int(gmsh.model.mesh.getMaxNodeTag())
            maximum_element_tag = int(gmsh.model.mesh.getMaxElementTag())
            gmsh.model.removePhysicalGroups([(3, int(fluid_group))])
            gmsh.model.geo.remove(
                [(3, int(tag)) for tag in prism_by_wall.values()],
                recursive=False,
            )
            gmsh.model.geo.synchronize()
            gmsh.model.mesh.clear([(3, core1_tag), (3, core2_tag)])
            # Clearing the two core volumes can also discard their orphaned
            # surface discretization after the temporary prism entities are
            # removed.  Restore the frozen prism/core top triangles before the
            # core fill so HXT consumes exactly the Pilot node identities.
            current_nodes_before_core = set(_nodes_from_gmsh(gmsh))
            for tag, records in interface_surface_snapshots.items():
                existing = _element_records_from_gmsh(gmsh, 2, tag)
                if existing == records:
                    continue
                if existing:
                    gmsh.model.mesh.clear([(2, tag)])
                required_nodes = {
                    int(node) for record in records for node in record["nodes"]
                }
                nodes = sorted(required_nodes - current_nodes_before_core)
                if nodes:
                    gmsh.model.mesh.addNodes(
                        2, tag, nodes,
                        [value for node in nodes for value in all_coordinates[node]],
                    )
                    current_nodes_before_core.update(nodes)
                by_type: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for record in records:
                    by_type[int(record["element_type"])].append(record)
                for element_type, typed_records in sorted(by_type.items()):
                    gmsh.model.mesh.addElementsByType(
                        tag,
                        element_type,
                        [int(record["tag"]) for record in typed_records],
                        [
                            int(node)
                            for record in typed_records
                            for node in record["nodes"]
                        ],
                    )
            gmsh.option.setNumber("Mesh.MeshOnlyEmpty", 1)
            gmsh.option.setNumber("Mesh.FirstNodeTag", maximum_node_tag + 1)
            gmsh.option.setNumber("Mesh.FirstElementTag", maximum_element_tag + 1)
            try:
                gmsh.model.mesh.generate(3)
                gmsh.model.mesh.optimize(
                    "Netgen",
                    force=True,
                    niter=10,
                    dimTags=[(3, core1_tag), (3, core2_tag)],
                )
            finally:
                gmsh.model.mesh.removeSizeCallback()
                gmsh.option.setNumber("Mesh.MeshOnlyEmpty", 0)
            # HXT may recreate the core side of an orphaned prism/core surface
            # with coordinate-identical but differently tagged nodes.  Merge
            # those tags deterministically back onto the frozen Pilot surface
            # before restoring the prism volumes, without moving any node.
            frozen_interface_nodes = {
                int(node)
                for records in interface_surface_snapshots.values()
                for record in records
                for node in record["nodes"]
            }
            fresh_coordinates = _nodes_from_gmsh(gmsh)
            fresh_by_coordinate: dict[tuple[str, str, str], list[int]] = defaultdict(list)
            for node, coordinates in fresh_coordinates.items():
                fresh_by_coordinate[
                    tuple(float(value).hex() for value in coordinates)
                ].append(int(node))
            interface_replacements: dict[int, int] = {}
            for frozen_node in sorted(frozen_interface_nodes):
                key = tuple(
                    float(value).hex() for value in all_coordinates[frozen_node]
                )
                candidates = fresh_by_coordinate.get(key, [])
                if not candidates:
                    raise BoundaryLayerSmokeError(
                        "production core lost a frozen interface coordinate"
                    )
                selected_node = (
                    frozen_node if frozen_node in candidates else min(candidates)
                )
                interface_replacements[selected_node] = frozen_node
            remapped_core: dict[int, list[dict[str, Any]]] = {}
            for core_tag in (core1_tag, core2_tag):
                records = _element_records_from_gmsh(gmsh, 3, core_tag)
                if not records or any(record["type"] != "Tetrahedron 4" for record in records):
                    raise BoundaryLayerSmokeError("production core snapshot is not Tet4-only")
                remapped_core[core_tag] = [
                    {
                        **record,
                        "nodes": [
                            interface_replacements.get(int(node), int(node))
                            for node in record["nodes"]
                        ],
                    }
                    for record in records
                ]
            gmsh.model.mesh.clear([(3, core1_tag), (3, core2_tag)])
            for tag, records in interface_surface_snapshots.items():
                gmsh.model.mesh.clear([(2, tag)])
                required_nodes = {
                    int(node) for record in records for node in record["nodes"]
                }
                present_nodes = set(_nodes_from_gmsh(gmsh))
                nodes = sorted(required_nodes - present_nodes)
                if nodes:
                    gmsh.model.mesh.addNodes(
                        2, tag, nodes,
                        [value for node in nodes for value in all_coordinates[node]],
                    )
                by_type: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for record in records:
                    by_type[int(record["element_type"])].append(record)
                for element_type, typed_records in sorted(by_type.items()):
                    gmsh.model.mesh.addElementsByType(
                        tag,
                        element_type,
                        [int(record["tag"]) for record in typed_records],
                        [int(node) for record in typed_records for node in record["nodes"]],
                    )
            tetra_type = int(gmsh.model.mesh.getElementType("tetrahedron", 1))
            for core_tag, records in remapped_core.items():
                gmsh.model.mesh.addElementsByType(
                    core_tag,
                    tetra_type,
                    [int(record["tag"]) for record in records],
                    [int(node) for record in records for node in record["nodes"]],
                )
            prism_type = int(gmsh.model.mesh.getElementType("prism", 1))
            current_nodes = set(_nodes_from_gmsh(gmsh))
            current_curves = {int(tag) for _dim, tag in gmsh.model.getEntities(1)}
            for tag, records in curve_snapshots.items():
                if tag in current_curves:
                    gmsh.model.mesh.clear([(1, tag)])
                else:
                    gmsh.model.addDiscreteEntity(1, tag=tag)
                required_nodes = {
                    int(node) for record in records for node in record["nodes"]
                }
                nodes = sorted(required_nodes - current_nodes)
                if nodes:
                    gmsh.model.mesh.addNodes(
                        1, tag, nodes,
                        [value for node in nodes for value in all_coordinates[node]],
                    )
                    current_nodes.update(nodes)
                by_type: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for record in records:
                    by_type[int(record["element_type"])].append(record)
                for element_type, typed_records in sorted(by_type.items()):
                    gmsh.model.mesh.addElementsByType(
                        tag,
                        element_type,
                        [int(record["tag"]) for record in typed_records],
                        [int(node) for record in typed_records for node in record["nodes"]],
                    )
            current_surfaces = {int(tag) for _dim, tag in gmsh.model.getEntities(2)}
            for tag, records in surface_snapshots.items():
                if tag in current_surfaces:
                    gmsh.model.mesh.clear([(2, tag)])
                else:
                    gmsh.model.addDiscreteEntity(2, tag=tag)
                required_nodes = {
                    int(node) for record in records for node in record["nodes"]
                }
                nodes = sorted(required_nodes - current_nodes)
                if nodes:
                    gmsh.model.mesh.addNodes(
                        2, tag, nodes,
                        [value for node in nodes for value in all_coordinates[node]],
                    )
                    current_nodes.update(nodes)
                by_type: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for record in records:
                    by_type[int(record["element_type"])].append(record)
                for element_type, typed_records in sorted(by_type.items()):
                    gmsh.model.mesh.addElementsByType(
                        tag,
                        element_type,
                        [int(record["tag"]) for record in typed_records],
                        [
                            int(node)
                            for record in typed_records
                            for node in record["nodes"]
                        ],
                    )
            for tag in sorted(prism_snapshots):
                gmsh.model.addDiscreteEntity(3, tag=tag)
                snapshot = prism_snapshots[tag]
                records = snapshot["records"]
                required_nodes = {
                    int(node) for record in records for node in record["nodes"]
                }
                nodes = sorted(required_nodes - current_nodes)
                if nodes:
                    gmsh.model.mesh.addNodes(
                        3,
                        tag,
                        nodes,
                        [value for node in nodes for value in all_coordinates[node]],
                    )
                    current_nodes.update(nodes)
                gmsh.model.mesh.addElementsByType(
                    tag,
                    prism_type,
                    [int(record["tag"]) for record in records],
                    [int(node) for record in records for node in record["nodes"]],
                )
            fluid_group = gmsh.model.addPhysicalGroup(
                3, [core1_tag, core2_tag, *[int(tag) for tag in prism_by_wall.values()]]
            )
            gmsh.model.setPhysicalName(3, fluid_group, fluid_name)
            core_count = sum(
                len(_element_records_from_gmsh(gmsh, 3, int(tag)))
                for tag in (core1_tag, core2_tag)
            )
            prism_count = sum(
                len(_element_records_from_gmsh(gmsh, 3, int(tag)))
                for tag in prism_by_wall.values()
            )
            production_volume_regions = {
                "schema": "cfdpipe.production_occ_core_hxt.v1",
                "status": "PASS",
                "face_nodes_added": 0,
                "prism_core_interface_preserved": True,
                "final_core_tetra_count": core_count,
                "prism_count": prism_count,
                "final_total_3d_element_count": core_count + prism_count,
                "volume_regions": region_evidence,
            }
            remeshed_records = _all_volume_records_for_entities(
                gmsh,
                [
                    core1_tag,
                    core2_tag,
                    *[int(tag) for tag in sorted(prism_by_wall.values())],
                ],
            )
            remeshed_nonprism = Counter(
                str(record["type"])
                for record in remeshed_records
                if record["type"] != "Prism 6"
            )
            frozen_connectivity = str(
                post_mesh_repair_plan.get("connectivity_sha256", "")
            )
            frozen_nonprism = copy.deepcopy(
                post_mesh_repair_plan.get("source_nonprism_type_counts")
            )
            post_mesh_repair_plan["source_nonprism_type_counts"] = dict(
                sorted(remeshed_nonprism.items())
            )
            post_mesh_repair_plan["connectivity_sha256"] = (
                _mesh_connectivity_sha256(gmsh)
            )
            post_mesh_repair_evidence = {
                **dict(post_mesh_repair_evidence),
                "production_core_remesh": {
                    "schema": "cfdpipe.production_core_remesh.v1",
                    "status": "PASS",
                    "prism_topology_preserved": True,
                    "frozen_connectivity_sha256": frozen_connectivity,
                    "production_connectivity_sha256": post_mesh_repair_plan[
                        "connectivity_sha256"
                    ],
                    "frozen_nonprism_type_counts": frozen_nonprism,
                    "production_nonprism_type_counts": dict(
                        sorted(remeshed_nonprism.items())
                    ),
                    "tetrahedral_refinement": production_volume_regions,
                },
            }
        prism_dimtags = [(3, int(tag)) for tag in sorted(prism_by_wall.values())]
        prism_quality_generation = _quality_summary_for_entities(
            gmsh, prism_dimtags, element_name="Prism 6"
        )

        wall_surface_tags_by_fingerprint = {
            fingerprint: [tag]
            for tag, fingerprint in wall_fingerprint_by_tag.items()
        }

        plan = {
            "core1_tag": core1_tag,
            "core2_tag": core2_tag,
            "prism_volume_tags": sorted(prism_by_wall.values()),
            "marker_surface_tags": marker_surface_tags,
            "shared_surface_tags": shared_tags,
            "wall_fingerprint_by_tag": wall_fingerprint_by_tag,
            "wall_surface_tags_by_fingerprint": wall_surface_tags_by_fingerprint,
            "collar_surface_tags": normalized_collar,
            "collar_base_surface_tags": normalized_collar_base,
            "top_surface_tags": sorted(top_by_wall.values()),
            "lateral_surface_tags": lateral_tags,
            "transition_surface_tags": transition_tags,
            "direction_audit": directions,
            "original_volume_tags": [core1_original, core2_original],
            "fluid_physical_name": fluid_name,
            "max_3d_elements": int(normalized_config["max_3d_elements"]),
            "local_refinement": refinement_audit,
            "mesh_options": fixed_options,
            "production_volume_regions": production_volume_regions,
            "prism_quality_generation": prism_quality_generation,
            "quality_improvement": dict(quality_improvement),
            "post_mesh_repair_plan": post_mesh_repair_plan,
            "post_mesh_repair_evidence": post_mesh_repair_evidence,
        }
        self._readback_plan = plan
        return self._payload(gmsh, plan, phase="build")

    def readback(
        self,
        gmsh: Any,
        mesh_path: Path,
        normalized_config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del mesh_path, normalized_config
        if self._readback_plan is None:
            raise BoundaryLayerSmokeError("readback has no successful build plan")
        return self._payload(gmsh, self._readback_plan, phase="readback")


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    return not bool(metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _snapshot(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "read_only": _is_read_only(path),
    }


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _write_new_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _audit_signature(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: audit.get(key)
        for key in (
            "node_count",
            "element_count_3d",
            "element_types_3d",
            "external_face_count",
            "marker_face_counts",
            "shared_interface_face_count",
            "shared_interface_region_pairs",
            "requested_layer_count",
            "layer_audit",
        )
    }


def _quality_gate_signature(evidence: Mapping[str, Any]) -> dict[str, Any]:
    gate = evidence.get("quality_improvement")
    if not isinstance(gate, Mapping):
        raise BoundaryLayerSmokeError("strategy quality-improvement evidence is missing")
    core = gate.get("core_tetra")
    prism = gate.get("prism_columns")
    if not isinstance(core, Mapping) or not isinstance(prism, Mapping):
        raise BoundaryLayerSmokeError("strategy quality-improvement evidence is incomplete")
    regions = core.get("regions")
    if not isinstance(regions, Mapping):
        raise BoundaryLayerSmokeError("core tetra region evidence is missing")
    return {
        "status": gate.get("status"),
        "core_status": core.get("status"),
        "core_below_gamma_count": core.get("below_gamma_count"),
        "core_regions": {
            str(role): {
                "element_count": record.get("element_count"),
                "bad_element_count_union": record.get("bad_element_count_union"),
                "below_threshold_count": record.get("below_threshold_count"),
            }
            for role, record in sorted(regions.items())
            if isinstance(record, Mapping)
        },
        "prism_status": prism.get("status"),
        "prism_element_count": prism.get("element_count"),
        "prism_column_count": prism.get("column_count"),
        "prism_below_threshold_element_count": prism.get(
            "below_threshold_element_count"
        ),
        "prism_below_threshold_column_count": prism.get(
            "below_threshold_column_count"
        ),
    }


def _call_audit(payload: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    required = {
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
    }
    if set(payload) != required:
        raise BoundaryLayerSmokeError("strategy audit payload is incomplete")
    contract_mode = str(config.get("contract_mode", "smoke"))
    if config.get("production_replay_after_audit") is True:
        # The frozen Pilot strategy deliberately retains its audit-only mode as
        # provenance.  A write-capable production replay nevertheless needs the
        # same >=15-layer mixed-mesh contract used by coarse calibration.
        contract_mode = "coarse_calibration"
    return audit_mixed_mesh(
        **payload,
        wall_fingerprints=config["wall_surface_fingerprints"],
        expected_marker_names=config["solver_marker_names"],
        requested_layers=config["layer_count"],
        max_elements=config["max_3d_elements"],
        contract_mode=contract_mode,
    )


def _session(
    gmsh: Any,
    action: Any,
    manifest: dict[str, Any],
    phase: str,
) -> Any:
    initialized = False
    attempted = False
    logger_started = False
    result: Any = None
    primary: BaseException | None = None
    primary_traceback = None
    try:
        is_initialized = getattr(gmsh, "isInitialized", None)
        if callable(is_initialized) and bool(is_initialized()):
            raise BoundaryLayerSmokeError(f"{phase} requires a fresh Gmsh session")
        attempted = True
        gmsh.initialize(readConfigFiles=False)
        initialized = True
        if getattr(gmsh, "logger", None) is not None:
            gmsh.logger.start()
            logger_started = True
        result = action()
    except BaseException as error:
        primary = error
        primary_traceback = error.__traceback__

    cleanup_errors: list[str] = []
    if logger_started:
        try:
            messages = [str(value) for value in gmsh.logger.get()]
            manifest["gmsh_sessions"][phase]["logger_messages"] = messages
        except BaseException as error:
            cleanup_errors.append(f"logger.get: {type(error).__name__}: {error}")
        try:
            gmsh.logger.stop()
        except BaseException as error:
            cleanup_errors.append(f"logger.stop: {type(error).__name__}: {error}")
    must_finalize = initialized
    if attempted and not must_finalize:
        try:
            is_initialized = getattr(gmsh, "isInitialized", None)
            must_finalize = callable(is_initialized) and bool(is_initialized())
        except BaseException as error:
            cleanup_errors.append(f"isInitialized: {type(error).__name__}: {error}")
    if must_finalize:
        try:
            gmsh.finalize()
            manifest["gmsh_sessions"][phase]["finalize_called"] = True
        except BaseException as error:
            cleanup_errors.append(f"finalize: {type(error).__name__}: {error}")
    manifest["gmsh_sessions"][phase]["cleanup_errors"] = cleanup_errors
    if primary is not None:
        raise primary.with_traceback(primary_traceback)
    if cleanup_errors:
        raise BoundaryLayerSmokeError(
            f"{phase} Gmsh session cleanup failed: {'; '.join(cleanup_errors)}"
        )
    return result


_HARD_FATAL_GMSH_LOG = re.compile(
    r"\b(?:fatal|segmentation fault|inverted|nan|inf(?:inity)?)\b"
    r"|negative\s+(?:jacobian|volume)",
    re.IGNORECASE,
)
_ERROR_GMSH_LOG = re.compile(r"\berrors?\b", re.IGNORECASE)
_ERROR_SEVERITY_GMSH_LOG = re.compile(r"^\s*error\s*:", re.IGNORECASE)
_WARNING_SEVERITY_GMSH_LOG = re.compile(r"^\s*warning\s*:", re.IGNORECASE)
_ILL_SHAPED_TET_WARNING = re.compile(
    r"^\s*warning\s*:\s*(?P<count>[1-9][0-9]*)\s+ill-shaped\s+"
    r"(?:tet\s+is|tets\s+are)\s+still\s+in\s+the\s+mesh\s*$",
    re.IGNORECASE,
)
_GMSH_ERROR_SUMMARY = re.compile(
    r"^\s*warning\s*:\s*mesh generation error summary\s*$", re.IGNORECASE
)
_GMSH_ZERO_ERRORS = re.compile(
    r"^\s*warning\s*:\s*0\s+errors?\s*$", re.IGNORECASE
)


def _strict_positive_quality_guard(audit: Mapping[str, Any] | None) -> dict[str, Any]:
    required_minima = ("volume", "minDetJac", "minSJ", "minSICN")
    required_zero_counts = (
        "nonpositive_volume_count",
        "nonpositive_jacobian_count",
        "nonfinite_count",
    )
    quality = audit.get("quality") if isinstance(audit, Mapping) else None
    result: dict[str, Any] = {
        "audit_status": audit.get("status") if isinstance(audit, Mapping) else None,
        "required_positive_minima": {},
        "required_zero_counts": {},
        "status": "FAIL",
    }
    if result["audit_status"] != "PASS" or not isinstance(quality, Mapping):
        return result
    for name in required_minima:
        try:
            value = float(quality.get(name))
        except (TypeError, ValueError):
            return result
        result["required_positive_minima"][name] = value
        if not math.isfinite(value) or value <= 0.0:
            return result
    for name in required_zero_counts:
        value = quality.get(name)
        result["required_zero_counts"][name] = value
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return result
    result["status"] = "PASS"
    return result


def _reject_fatal_gmsh_log(
    manifest: Mapping[str, Any],
    phase: str,
    *,
    defer_ill_shaped: bool = False,
) -> dict[str, Any]:
    session = manifest["gmsh_sessions"][phase]
    messages = [str(message) for message in session["logger_messages"]]
    zero_error_summary_present = any(_GMSH_ZERO_ERRORS.fullmatch(value) for value in messages)
    fatal: list[str] = []
    pending: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    for line_number, message in enumerate(messages, start=1):
        if _ERROR_SEVERITY_GMSH_LOG.search(message) or _HARD_FATAL_GMSH_LOG.search(message):
            fatal.append(message)
            continue
        ill_shaped = _ILL_SHAPED_TET_WARNING.fullmatch(message)
        if ill_shaped is not None:
            if phase != "build" or not defer_ill_shaped:
                fatal.append(message)
                continue
            pending.append(
                {
                    "code": "GMSH_ILL_SHAPED_TETRAHEDRA_PENDING_FINAL_AUDIT",
                    "phase": phase,
                    "line_number": line_number,
                    "raw_line": message,
                    "ill_shaped_tetrahedron_count": int(ill_shaped.group("count")),
                    "disposition": "PENDING_COMPLETE_SMOKE_GATE",
                }
            )
            continue
        if _WARNING_SEVERITY_GMSH_LOG.search(message):
            fatal.append(message)
            continue
        if _ERROR_GMSH_LOG.search(message):
            if _GMSH_ZERO_ERRORS.fullmatch(message) or (
                _GMSH_ERROR_SUMMARY.fullmatch(message) and zero_error_summary_present
            ):
                summary_lines.append(message)
                continue
            fatal.append(message)
    diagnostic_audit = {
        "status": "FAIL" if fatal else ("PENDING" if pending else "PASS"),
        "raw_messages_preserved": True,
        "fatal_messages": fatal,
        "pending_diagnostics": pending,
        "classified_nonfatal_diagnostics": [],
        "associated_summary_lines": summary_lines,
        "policy": (
            "only the exact build-phase positive-count ill-shaped tetrahedron warning may remain "
            "pending; every other Warning/Error/Fatal/inverted/nonfinite/negative diagnostic is fatal"
        ),
    }
    if isinstance(session, dict):
        session["diagnostic_audit"] = diagnostic_audit
    if fatal:
        raise BoundaryLayerSmokeError(
            f"Gmsh {phase} log contains fatal diagnostics: {fatal[0]}"
        )
    return diagnostic_audit


def _finalize_deferred_gmsh_log_diagnostics(
    manifest: dict[str, Any],
    generation: Mapping[str, Any],
    readback: Mapping[str, Any],
    serialized_su2: Mapping[str, Any],
    msh_path: Path,
    su2_path: Path,
) -> dict[str, Any]:
    build_diagnostic = manifest["gmsh_sessions"]["build"].get("diagnostic_audit", {})
    readback_diagnostic = manifest["gmsh_sessions"]["readback"].get(
        "diagnostic_audit", {}
    )
    pending = list(build_diagnostic.get("pending_diagnostics", []))
    if not pending:
        manifest["diagnostic_quality_status"] = "PASS"
        manifest["requires_quality_improvement_before_production"] = False
        return {"status": "PASS", "classified_nonfatal_diagnostics": []}

    build_quality = _strict_positive_quality_guard(generation)
    readback_quality = _strict_positive_quality_guard(readback)
    output_evidence = {
        "mesh_msh_sha256": _sha256(msh_path) if msh_path.is_file() else None,
        "mesh_su2_sha256": _sha256(su2_path) if su2_path.is_file() else None,
    }
    signature_matches = _audit_signature(generation) == _audit_signature(readback)
    su2_matches = (
        serialized_su2.get("status") == "PASS"
        and serialized_su2.get("nelem") == generation.get("element_count_3d")
        and serialized_su2.get("marker_element_counts")
        == generation.get("marker_face_counts")
    )
    sessions_complete = all(
        manifest["gmsh_sessions"][name].get("finalize_called") is True
        and manifest["gmsh_sessions"][name].get("cleanup_errors") == []
        for name in ("build", "readback")
    )
    strategy_evidence = manifest.get("strategy_evidence", {})
    build_strategy = (
        strategy_evidence.get("build", {})
        if isinstance(strategy_evidence, Mapping)
        else {}
    )
    readback_strategy = (
        strategy_evidence.get("readback", {})
        if isinstance(strategy_evidence, Mapping)
        else {}
    )
    hard_quality_gate_resolved = False
    hard_quality_signature_matches = False
    try:
        build_hard_quality = _quality_gate_signature(build_strategy)
        readback_hard_quality = _quality_gate_signature(readback_strategy)
        hard_quality_signature_matches = build_hard_quality == readback_hard_quality
        hard_quality_gate_resolved = (
            hard_quality_signature_matches
            and build_hard_quality.get("status") == "PASS"
            and build_hard_quality.get("core_status") == "PASS"
            and build_hard_quality.get("core_below_gamma_count") == 0
            and build_hard_quality.get("prism_status") == "PASS"
            and build_hard_quality.get("prism_below_threshold_element_count") == 0
            and build_hard_quality.get("prism_below_threshold_column_count") == 0
        )
    except BoundaryLayerSmokeError:
        build_hard_quality = None
        readback_hard_quality = None
    complete = all(
        (
            build_diagnostic.get("status") == "PENDING",
            not build_diagnostic.get("fatal_messages"),
            readback_diagnostic.get("status") == "PASS",
            not readback_diagnostic.get("fatal_messages"),
            build_quality["status"] == "PASS",
            readback_quality["status"] == "PASS",
            signature_matches,
            su2_matches,
            manifest.get("source_unchanged") is True,
            sessions_complete,
            manifest.get("production_mesh_eligible") is False,
            manifest.get("su2_called") is False,
            manifest.get("paraview_called") is False,
            manifest.get("external_commands") == [],
            output_evidence["mesh_msh_sha256"] is not None,
            output_evidence["mesh_su2_sha256"] is not None,
        )
    )
    final_evidence = {
        "build_quality_guard": build_quality,
        "readback_quality_guard": readback_quality,
        "build_readback_signature_matches": signature_matches,
        "su2_text_validation_status": serialized_su2.get("status"),
        "su2_element_and_marker_counts_match": su2_matches,
        "source_unchanged": manifest.get("source_unchanged"),
        "gmsh_sessions_finalized_without_cleanup_error": sessions_complete,
        "downstream_programs_not_called": (
            manifest.get("su2_called") is False
            and manifest.get("paraview_called") is False
            and manifest.get("external_commands") == []
        ),
        "output_hashes": output_evidence,
        "hard_quality_gate_resolved": hard_quality_gate_resolved,
        "hard_quality_signature_matches": hard_quality_signature_matches,
        "build_hard_quality_signature": build_hard_quality,
        "readback_hard_quality_signature": readback_hard_quality,
    }
    if not complete:
        build_diagnostic["status"] = "FAIL"
        build_diagnostic["deferred_classification_evidence"] = final_evidence
        manifest["diagnostic_quality_status"] = "FAIL"
        manifest["requires_quality_improvement_before_production"] = True
        raise BoundaryLayerSmokeError(
            "ill-shaped tetrahedron warning lacks complete final smoke-gate evidence"
        )

    if hard_quality_gate_resolved:
        classified = [
            {
                **dict(record),
                "code": (
                    "GMSH_GENERATION_WARNING_RESOLVED_BY_FINAL_HARD_QUALITY_GATE"
                ),
                "classification": "RESOLVED_PRE_FINAL_AUDIT_DIAGNOSTIC",
                "disposition": "PASS_AFTER_BUILD_AND_FRESH_READBACK_HARD_GATE",
                "evidence": final_evidence,
            }
            for record in pending
        ]
        build_diagnostic["status"] = "PASS"
        build_diagnostic["pending_diagnostics"] = []
        build_diagnostic["classified_nonfatal_diagnostics"] = classified
        build_diagnostic["deferred_classification_evidence"] = final_evidence
        manifest["diagnostic_quality_status"] = "PASS"
        manifest["requires_quality_improvement_before_production"] = False
        return {"status": "PASS", "classified_nonfatal_diagnostics": classified}

    classified = [
        {
            **dict(record),
            "code": "GMSH_PRE_REPAIR_ILL_SHAPED_TETS_SUPERSEDED_BY_FINAL_STRICT_AUDIT",
            "classification": "NONFATAL_SMOKE_MESH_QUALITY_WARNING",
            "disposition": "WARN_AFTER_COMPLETE_SMOKE_GATE",
            "evidence": final_evidence,
        }
        for record in pending
    ]
    build_diagnostic["status"] = "WARN"
    build_diagnostic["pending_diagnostics"] = []
    build_diagnostic["classified_nonfatal_diagnostics"] = classified
    build_diagnostic["deferred_classification_evidence"] = final_evidence
    manifest["diagnostic_quality_status"] = "WARN"
    manifest["requires_quality_improvement_before_production"] = True
    return {"status": "WARN", "classified_nonfatal_diagnostics": classified}


def _strategy_evidence(strategy: Any, phase: str) -> dict[str, Any]:
    method = getattr(strategy, "evidence", None)
    if not callable(method):
        return {}
    value = method(phase)
    if not isinstance(value, Mapping):
        raise BoundaryLayerSmokeError("strategy evidence must be a mapping")
    return dict(value)


def build_boundary_layer_smoke(
    source_brep: PathLike,
    output_directory: PathLike,
    normalized_config: Mapping[str, Any],
    *,
    strategy: BoundaryLayerSmokeStrategy,
    gmsh_module: Any | None = None,
    run_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the injected complete-mesh strategy with two fresh Gmsh sessions."""

    if normalized_config.get("schema") != _SCHEMA:
        raise BoundaryLayerSmokeError("normalized boundary-layer smoke config is invalid")
    configured_config_hash = normalized_config.get("normalized_config_sha256")
    unsigned_config = dict(normalized_config)
    unsigned_config.pop("normalized_config_sha256", None)
    if (
        not isinstance(configured_config_hash, str)
        or _SHA256.fullmatch(configured_config_hash.casefold()) is None
        or _canonical_hash(unsigned_config) != configured_config_hash.casefold()
    ):
        raise BoundaryLayerSmokeError("normalized boundary-layer smoke config hash is stale")
    if normalized_config.get("su2_called") is not False or normalized_config.get(
        "paraview_called"
    ) is not False:
        raise BoundaryLayerSmokeError("downstream programs must remain disabled")
    if not callable(getattr(strategy, "build", None)) or not callable(
        getattr(strategy, "readback", None)
    ):
        raise BoundaryLayerSmokeError("complete build/readback strategy is required")

    source = Path(source_brep).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.casefold() != ".brep" or not _is_read_only(source):
        raise BoundaryLayerSmokeError("source must be an existing read-only BREP")
    configured_hash = normalized_config.get("provenance", {}).get("pipeline_brep_sha256")
    before = _snapshot(source)
    if before["sha256"] != configured_hash:
        raise BoundaryLayerSmokeError("source BREP SHA-256 differs from the contract")

    output = Path(os.path.abspath(Path(output_directory).expanduser()))
    if ".." in output.parts or output.exists():
        raise BoundaryLayerSmokeError("refusing an unsafe or existing output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".boundary-layer-smoke-{os.getpid()}-{uuid4().hex}"
    staging.mkdir()
    started = _utc_now()
    manifest: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL",
        "started_at_utc": started,
        "ended_at_utc": None,
        "smoke_only": True,
        "production_mesh_eligible": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "external_stderr": None,
        "source_before": before,
        "source_after": None,
        "source_unchanged": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_sessions": {
            "build": {
                "finalize_called": False,
                "logger_messages": [],
                "cleanup_errors": [],
            },
            "readback": {
                "finalize_called": False,
                "logger_messages": [],
                "cleanup_errors": [],
            },
        },
        "error": None,
        "normalized_config": dict(normalized_config),
        "normalized_config_sha256": configured_config_hash.casefold(),
        "run_evidence": dict(run_evidence or {}),
        "strategy_evidence": {"build": {}, "readback": {}},
        "output_directory": str(output),
    }
    log_lines = [f"[{started}] boundary-layer smoke started"]
    primary: BaseException | None = None
    rendered = ""
    try:
        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_file = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_file).resolve()) if module_file else "unknown"
        )
        msh_path = staging / "mesh.msh"
        su2_path = staging / "mesh.su2"

        def build_action() -> dict[str, Any]:
            gmsh.model.add("cfdpipe_boundary_layer_smoke")
            payload = strategy.build(gmsh, source, normalized_config, staging)
            audit = _call_audit(payload, normalized_config)
            gmsh.write(str(msh_path))
            gmsh.write(str(su2_path))
            return audit

        generation = _session(gmsh, build_action, manifest, "build")
        manifest["generation_audit"] = generation
        manifest["strategy_evidence"]["build"] = _strategy_evidence(strategy, "build")
        _reject_fatal_gmsh_log(manifest, "build", defer_ill_shaped=True)
        if not msh_path.is_file() or msh_path.stat().st_size <= 0:
            raise BoundaryLayerSmokeError("Gmsh did not write a nonempty MSH")
        serialized_su2 = parse_su2_mixed_mesh(
            su2_path,
            expected_markers=normalized_config["solver_marker_names"],
            max_elements=normalized_config["max_3d_elements"],
        )
        manifest["su2_validation"] = serialized_su2

        def readback_action() -> dict[str, Any]:
            gmsh.open(str(msh_path))
            payload = strategy.readback(gmsh, msh_path, normalized_config)
            return _call_audit(payload, normalized_config)

        readback = _session(gmsh, readback_action, manifest, "readback")
        manifest["readback_audit"] = readback
        manifest["strategy_evidence"]["readback"] = _strategy_evidence(
            strategy, "readback"
        )
        _reject_fatal_gmsh_log(manifest, "readback")
        if _audit_signature(generation) != _audit_signature(readback):
            raise BoundaryLayerSmokeError("fresh MSH readback changed audited topology")
        if _quality_gate_signature(
            manifest["strategy_evidence"]["build"]
        ) != _quality_gate_signature(manifest["strategy_evidence"]["readback"]):
            raise BoundaryLayerSmokeError(
                "fresh MSH readback changed the hard quality-gate signature"
            )
        after = _snapshot(source)
        manifest["source_after"] = after
        manifest["source_unchanged"] = after == before
        if not manifest["source_unchanged"]:
            raise BoundaryLayerSmokeError("source BREP changed during meshing")
        outputs = {
            "mesh.msh": {"path": str(output / "mesh.msh"), "sha256": _sha256(msh_path), "size_bytes": msh_path.stat().st_size},
            "mesh.su2": {"path": str(output / "mesh.su2"), "sha256": _sha256(su2_path), "size_bytes": su2_path.stat().st_size},
        }
        manifest["outputs"] = outputs
        _finalize_deferred_gmsh_log_diagnostics(
            manifest, generation, readback, serialized_su2, msh_path, su2_path
        )
        manifest.update(
            {
                "status": "PASS",
                "generation_audit": generation,
                "readback_audit": readback,
                "su2_validation": serialized_su2,
                "outputs": outputs,
            }
        )
    except BaseException as error:
        primary = error
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        log_lines.append(rendered)
        for phase in ("build", "readback"):
            try:
                evidence = _strategy_evidence(strategy, phase)
                if evidence:
                    manifest["strategy_evidence"][phase] = evidence
            except BaseException as evidence_error:
                manifest["strategy_evidence"][phase] = {
                    "evidence_error": f"{type(evidence_error).__name__}: {evidence_error}"
                }
    if manifest["source_after"] is None:
        try:
            after = _snapshot(source)
            manifest["source_after"] = after
            manifest["source_unchanged"] = after == before
        except BaseException as error:
            manifest["source_snapshot_error"] = (
                f"{type(error).__name__}: {error}"
            )
    manifest["ended_at_utc"] = _utc_now()
    for phase in ("build", "readback"):
        for message in manifest["gmsh_sessions"][phase]["logger_messages"]:
            log_lines.append(f"[gmsh:{phase}] {message}")
    if primary is None:
        try:
            log_lines.append(f"[{manifest['ended_at_utc']}] status=PASS")
            _write_new_text(
                staging / "gmsh_boundary_layer_smoke.log",
                "\n".join(log_lines) + "\n",
            )
            _write_new_json(staging / "boundary_layer_smoke_manifest.json", manifest)
            staging.rename(output)
            return manifest
        except BaseException as error:
            primary = error
            rendered = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            manifest["status"] = "FAIL"
            manifest["ended_at_utc"] = _utc_now()
            log_lines.append(rendered)

    manifest["error"] = {
        "type": type(primary).__name__,
        "message": str(primary),
        "traceback": rendered,
    }
    log_lines.append(f"[{manifest['ended_at_utc']}] status=FAIL")
    if staging.exists():
        shutil.rmtree(staging)
    if output.exists():
        raise BoundaryLayerSmokeError(
            "boundary-layer smoke publication failed and output was claimed concurrently"
        ) from primary
    output.mkdir()
    _write_new_text(output / "gmsh_boundary_layer_smoke.log", "\n".join(log_lines) + "\n")
    _write_new_json(output / "boundary_layer_smoke_manifest.json", manifest)
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        raise primary
    raise BoundaryLayerSmokeError(
        f"boundary-layer smoke mesh failed: {manifest['error']['message']}"
    ) from primary


__all__ = [
    "BoundaryLayerSmokeError",
    "BoundaryLayerSmokeStrategy",
    "RealProjectBoundaryLayerStrategy",
    "audit_mixed_mesh",
    "build_boundary_layer_smoke",
    "normalize_boundary_layer_smoke_config",
    "parse_su2_mixed_mesh",
]
