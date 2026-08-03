"""Read-only wall-normal clearance evidence for coarse boundary-layer planning.

The audit deliberately stops short of meshing or extrusion.  It imports the
hash-bound pipeline BREP in one fresh Gmsh session, rematches the 48 wall
surfaces by their stable fingerprints, reuses the existing five-sample inward
direction proof, and brackets the first *detected* exit from the adjacent fluid
volume along each sampled inward normal.

Passing this audit means only that the sampled conservative clearance bounds
exceed the requested layer-stack thickness.  It is not proof that a valid
boundary-layer mesh can be extruded between the samples or around sharp edges.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
import operator
from pathlib import Path
import stat
import tomllib
import traceback
from typing import Any, Callable, Mapping, Sequence

from .boundary_layer_smoke import _audit_wall_inward_direction
from .geometry_repair import _collect_model
from .topology_smoke_mesh import _normalize_contract, _resolve_entities


_SCHEMA = "cfdpipe.boundary_layer_clearance.v1"
_COARSE_SCHEMA = "cfdpipe.coarse_mesh.v1"
_CLEARANCE_EVIDENCE_CONTRACT_SCHEMA = (
    "cfdpipe.boundary_layer_clearance_evidence_contract.v1"
)
_REQUIRED_WALL_SURFACE_COUNT = 48
_SHA256_LENGTH = 64


class BoundaryLayerClearanceError(RuntimeError):
    """Raised when clearance evidence cannot be obtained unambiguously."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerClearanceError(f"{label} must be finite") from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "finite and positive" if positive else "finite"
        raise BoundaryLayerClearanceError(f"{label} must be {qualifier}")
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise BoundaryLayerClearanceError(f"{label} must be a positive integer")
    try:
        result = int(operator.index(value))  # type: ignore[arg-type]
    except TypeError as error:
        raise BoundaryLayerClearanceError(
            f"{label} must be a positive integer"
        ) from error
    if result <= 0:
        raise BoundaryLayerClearanceError(f"{label} must be a positive integer")
    return result


def _vector(value: object, length: int, label: str) -> list[float]:
    if isinstance(value, (str, bytes, Mapping)):
        raise BoundaryLayerClearanceError(f"{label} must contain {length} values")
    try:
        raw = list(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise BoundaryLayerClearanceError(
            f"{label} must contain {length} values"
        ) from error
    if len(raw) != length:
        raise BoundaryLayerClearanceError(f"{label} must contain {length} values")
    return [_finite(item, f"{label}[{index}]") for index, item in enumerate(raw)]


def _inside_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        result = int(operator.index(value))  # type: ignore[arg-type]
    except TypeError as error:
        raise BoundaryLayerClearanceError(
            "gmsh.model.isInside returned a non-integer count"
        ) from error
    if result < 0:
        raise BoundaryLayerClearanceError(
            "gmsh.model.isInside returned a negative count"
        )
    return result


def _point_on_ray(
    point: Sequence[float], unit_direction: Sequence[float], distance: float
) -> list[float]:
    return [
        float(point[index]) + distance * float(unit_direction[index])
        for index in range(3)
    ]


def probe_clearance(
    is_inside: Callable[[Sequence[float]], object],
    point_m: Sequence[float],
    inward_unit_normal: Sequence[float],
    initial_inside_distance_m: float,
    maximum_distance_m: float,
    *,
    absolute_tolerance_m: float | None = None,
    maximum_iterations: int = 96,
) -> dict[str, Any]:
    """Bracket a ray's first detected inside-to-outside transition.

    The search first doubles the distance from a point already proven inside,
    then bisects the first pair of sampled distances whose classifications
    differ.  For a non-convex volume this is explicitly a sampled, first-
    detected transition; no claim is made about unsampled gaps or extrusion
    feasibility.
    """

    if not callable(is_inside):
        raise BoundaryLayerClearanceError("is_inside must be callable")
    point = _vector(point_m, 3, "clearance point")
    direction = _vector(inward_unit_normal, 3, "inward unit normal")
    magnitude = math.sqrt(sum(component * component for component in direction))
    if not math.isclose(magnitude, 1.0, rel_tol=1.0e-10, abs_tol=1.0e-12):
        raise BoundaryLayerClearanceError("inward normal is not a unit vector")
    initial = _finite(
        initial_inside_distance_m, "initial inside distance", positive=True
    )
    maximum = _finite(maximum_distance_m, "maximum distance", positive=True)
    if initial >= maximum:
        raise BoundaryLayerClearanceError(
            "initial inside distance must be below maximum distance"
        )
    iterations = _positive_integer(maximum_iterations, "maximum iterations")
    if absolute_tolerance_m is None:
        tolerance = max(initial * 1.0e-8, maximum * 1.0e-12)
    else:
        tolerance = _finite(
            absolute_tolerance_m, "absolute clearance tolerance", positive=True
        )
    if tolerance >= maximum - initial:
        raise BoundaryLayerClearanceError(
            "absolute clearance tolerance is too large for the search interval"
        )

    probes: list[dict[str, Any]] = []

    def classify(distance: float, phase: str) -> int:
        query = _point_on_ray(point, direction, distance)
        count = _inside_count(is_inside(query))
        probes.append(
            {
                "phase": phase,
                "distance_m": distance,
                "inside_count": count,
                "point_m": query,
            }
        )
        return count

    if classify(initial, "initial") <= 0:
        raise BoundaryLayerClearanceError(
            "initial clearance probe is not inside the adjacent fluid volume"
        )

    lower = initial
    upper: float | None = None
    expansion_iterations = 0
    for _ in range(iterations):
        candidate = min(maximum, lower * 2.0)
        if candidate <= lower:
            break
        expansion_iterations += 1
        if classify(candidate, "expansion") <= 0:
            upper = candidate
            break
        lower = candidate
        if lower >= maximum:
            break
    if upper is None:
        raise BoundaryLayerClearanceError(
            "failed to bracket a wall-normal volume exit within maximum distance"
        )

    bisection_iterations = 0
    while upper - lower > tolerance:
        if bisection_iterations >= iterations:
            raise BoundaryLayerClearanceError(
                "wall-normal clearance bisection did not converge"
            )
        midpoint = lower + 0.5 * (upper - lower)
        bisection_iterations += 1
        if classify(midpoint, "bisection") > 0:
            lower = midpoint
        else:
            upper = midpoint

    return {
        "status": "PASS",
        "method": "doubling_then_bisection_first_detected_inside_to_outside",
        "first_exit_guaranteed_for_nonconvex_volume": False,
        "point_m": point,
        "inward_unit_normal": direction,
        "initial_inside_distance_m": initial,
        "maximum_distance_m": maximum,
        "absolute_tolerance_m": tolerance,
        "clearance_lower_bound_m": lower,
        "clearance_upper_bound_m": upper,
        "clearance_midpoint_m": lower + 0.5 * (upper - lower),
        "bracket_width_m": upper - lower,
        "expansion_iterations": expansion_iterations,
        "bisection_iterations": bisection_iterations,
        "probes": probes,
    }


def probe_normal_clearance(
    gmsh: Any,
    *,
    volume_tag: int,
    point_m: Sequence[float],
    inward_unit_normal: Sequence[float],
    initial_inside_distance_m: float,
    maximum_distance_m: float,
    absolute_tolerance_m: float,
    maximum_expansion_iterations: int = 96,
    maximum_bisection_iterations: int = 96,
) -> dict[str, Any]:
    """Use ``gmsh.model.isInside`` as the classifier for ``probe_clearance``."""

    tag = _positive_integer(volume_tag, "adjacent volume tag")
    expansion_limit = _positive_integer(
        maximum_expansion_iterations, "maximum expansion iterations"
    )
    bisection_limit = _positive_integer(
        maximum_bisection_iterations, "maximum bisection iterations"
    )
    # The pure routine uses one bound for each phase.  Taking the smaller value
    # honors both explicit caller budgets without weakening either limit.
    limit = min(expansion_limit, bisection_limit)

    def is_inside(point: Sequence[float]) -> object:
        return gmsh.model.isInside(3, tag, list(point))

    return probe_clearance(
        is_inside,
        point_m,
        inward_unit_normal,
        initial_inside_distance_m,
        maximum_distance_m,
        absolute_tolerance_m=absolute_tolerance_m,
        maximum_iterations=limit,
    )


def audit_clearance_samples(
    samples: Sequence[Mapping[str, Any]],
    *,
    required_total_thickness_m: float,
) -> dict[str, Any]:
    """Compare completed sample probes with the required layer-stack height."""

    required = _finite(
        required_total_thickness_m, "required total thickness", positive=True
    )
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise BoundaryLayerClearanceError("clearance samples must be a sequence")
    compared: list[dict[str, Any]] = []
    for index, raw in enumerate(samples):
        if not isinstance(raw, Mapping):
            raise BoundaryLayerClearanceError(
                f"clearance sample {index} must be a mapping"
            )
        lower = _finite(
            raw.get("clearance_lower_bound_m"),
            f"clearance sample {index} lower bound",
            positive=True,
        )
        upper = _finite(
            raw.get("clearance_upper_bound_m"),
            f"clearance sample {index} upper bound",
            positive=True,
        )
        if lower > upper:
            raise BoundaryLayerClearanceError(
                f"clearance sample {index} has an inverted bracket"
            )
        record = dict(raw)
        record["required_total_thickness_m"] = required
        record["conservative_clearance_margin_m"] = lower - required
        record["required_total_thickness_clear"] = lower >= required
        compared.append(record)
    if not compared:
        raise BoundaryLayerClearanceError("clearance samples are empty")
    minimum_index = min(
        range(len(compared)),
        key=lambda index: float(compared[index]["clearance_lower_bound_m"]),
    )
    minimum = compared[minimum_index]
    passed = all(
        bool(record["required_total_thickness_clear"]) for record in compared
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "required_total_thickness_m": required,
        "sample_count": len(compared),
        "minimum_clearance_lower_bound_m": minimum[
            "clearance_lower_bound_m"
        ],
        "minimum_clearance_sample_index": minimum_index,
        "minimum_conservative_margin_m": minimum[
            "conservative_clearance_margin_m"
        ],
        "samples": compared,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise BoundaryLayerClearanceError(
            "coarse contract is not canonically serializable"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    junction = getattr(path, "is_junction", None)
    return (
        path.is_symlink()
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        or (bool(junction()) if callable(junction) else False)
    )


def _has_link_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_link_like(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _snapshot(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "read_only": _is_read_only(path),
    }


def _validated_clearance_evidence_contract(
    coarse_contract: Mapping[str, Any],
    marker_config: Mapping[str, Any],
    source: Path,
) -> dict[str, Any]:
    """Validate the clearance-only sub-contract against current source files."""

    record = coarse_contract.get("clearance_evidence_contract")
    digest = coarse_contract.get("clearance_evidence_contract_sha256")
    if (
        not isinstance(record, Mapping)
        or record.get("schema") != _CLEARANCE_EVIDENCE_CONTRACT_SCHEMA
        or not _is_sha256(digest)
        or _canonical_hash(record) != digest
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence sub-contract is missing or stale"
        )
    if set(record) != {
        "schema",
        "source_geometry",
        "marker_config",
        "wall_selection",
        "clearance_requirement",
        "audit_policy",
        "scope",
    }:
        raise BoundaryLayerClearanceError(
            "clearance evidence sub-contract root is incomplete"
        )

    source_record = record.get("source_geometry")
    if not isinstance(source_record, Mapping) or set(source_record) != {
        "path",
        "sha256",
        "required_suffix",
        "read_only_required",
        "links_allowed",
    }:
        raise BoundaryLayerClearanceError(
            "clearance evidence source identity is incomplete"
        )
    try:
        contract_source = Path(str(source_record.get("path", ""))).resolve(
            strict=True
        )
    except OSError as error:
        raise BoundaryLayerClearanceError(
            "clearance evidence source no longer exists"
        ) from error
    if (
        contract_source != source
        or source_record.get("sha256") != _sha256(source)
        or source_record.get("required_suffix") != ".brep"
        or source_record.get("read_only_required") is not True
        or source_record.get("links_allowed") is not False
        or not _is_read_only(source)
        or _has_link_component(source)
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence source identity is stale or unsafe"
        )

    marker_record = record.get("marker_config")
    if not isinstance(marker_record, Mapping) or set(marker_record) != {
        "path",
        "sha256",
        "pipeline_geometry_sha256",
        "coordinate_absolute_tolerance_m",
    }:
        raise BoundaryLayerClearanceError(
            "clearance evidence marker identity is incomplete"
        )
    marker_candidate = Path(str(marker_record.get("path", "")))
    try:
        marker_path = marker_candidate.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceError(
            "clearance evidence marker config no longer exists"
        ) from error
    if (
        not marker_path.is_file()
        or marker_path.suffix.casefold() != ".toml"
        or _has_link_component(marker_candidate.absolute())
        or marker_record.get("sha256") != _sha256(marker_path)
        or marker_record.get("pipeline_geometry_sha256") != _sha256(source)
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence marker identity is stale or unsafe"
        )
    try:
        with marker_path.open("rb") as stream:
            current_marker_config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BoundaryLayerClearanceError(
            "clearance evidence marker config cannot be parsed"
        ) from error
    if current_marker_config != marker_config:
        raise BoundaryLayerClearanceError(
            "supplied marker config differs from the hash-bound marker file"
        )
    marker_geometry = marker_config.get("pipeline_geometry")
    matching = marker_config.get("matching")
    if not isinstance(marker_geometry, Mapping) or not isinstance(
        matching, Mapping
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence marker geometry is incomplete"
        )
    coordinate_tolerance = _finite(
        matching.get("coordinate_absolute_tolerance_m"),
        "marker coordinate tolerance",
        positive=True,
    )
    if (
        marker_geometry.get("sha256") != _sha256(source)
        or marker_record.get("coordinate_absolute_tolerance_m")
        != coordinate_tolerance
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence marker geometry is stale"
        )

    design = coarse_contract.get("boundary_layer_design")
    base = coarse_contract.get("base_smoke_contract")
    fingerprints = coarse_contract.get("wall_surface_fingerprints")
    wall = record.get("wall_selection")
    requirement = record.get("clearance_requirement")
    policy = record.get("audit_policy")
    scope = record.get("scope")
    if (
        not isinstance(design, Mapping)
        or design.get("status") != "PASS"
        or not isinstance(base, Mapping)
        or not isinstance(fingerprints, list)
        or not isinstance(wall, Mapping)
        or not isinstance(requirement, Mapping)
        or not isinstance(policy, Mapping)
        or not isinstance(scope, Mapping)
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence audit inputs are incomplete"
        )
    normalized_fingerprints = sorted(str(value).casefold() for value in fingerprints)
    expected_scope = {
        "read_only_geometry_audit": True,
        "mesh_generated": False,
        "gmsh_write_called": False,
        "gui_started": False,
        "su2_called": False,
        "paraview_called": False,
        "extrusion_feasibility_claimed": False,
        "production_mesh_eligible": False,
    }
    expected_policy = {
        "samples_per_surface": 5,
        "total_sample_count": 240,
        "direction_probe_distance_m": _finite(
            base.get("direction_probe_distance_m"),
            "direction probe distance",
            positive=True,
        ),
        "direction_probe_coordinate_tolerance_multipliers": [
            5.0,
            50.0,
            500.0,
        ],
        "maximum_search_distance_volume_diagonal_multiplier": 2.0,
        "maximum_probe_iterations": 96,
        "absolute_tolerance_policy": (
            "min(marker_coordinate_absolute_tolerance_m,"
            "required_total_thickness_m*1e-6)"
        ),
        "transition_policy": (
            "doubling_then_bisection_first_detected_inside_to_outside"
        ),
        "nonconvex_first_exit_guaranteed": False,
    }
    required = _finite(
        design.get("total_thickness_m"),
        "required total thickness",
        positive=True,
    )
    if (
        set(wall) != {
            "physical_name",
            "stable_surface_fingerprint_count",
            "stable_surface_fingerprints",
        }
        or wall.get("physical_name") != base.get("wall_physical_name")
        or wall.get("stable_surface_fingerprint_count") != 48
        or wall.get("stable_surface_fingerprints") != normalized_fingerprints
        or set(requirement) != {"required_total_thickness_m"}
        or requirement.get("required_total_thickness_m") != required
        or dict(policy) != expected_policy
        or dict(scope) != expected_scope
    ):
        raise BoundaryLayerClearanceError(
            "clearance evidence audit policy differs from the current geometry inputs"
        )
    return {
        "sha256": str(digest),
        "required_total_thickness_m": required,
        "wall_physical_name": str(wall["physical_name"]),
        "wall_surface_fingerprints": normalized_fingerprints,
        "direction_probe_distance_m": float(
            policy["direction_probe_distance_m"]
        ),
        "coordinate_tolerance_m": coordinate_tolerance,
    }


def _validated_inputs(
    pipeline_brep: str | Path,
    coarse_contract: Mapping[str, Any],
    marker_config: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(coarse_contract, Mapping):
        raise BoundaryLayerClearanceError("coarse contract must be a mapping")
    if coarse_contract.get("schema") != _COARSE_SCHEMA:
        raise BoundaryLayerClearanceError("coarse contract schema is invalid")
    configured_hash = coarse_contract.get("normalized_config_sha256")
    unsigned = dict(coarse_contract)
    unsigned.pop("normalized_config_sha256", None)
    if not _is_sha256(configured_hash) or _canonical_hash(unsigned) != configured_hash:
        raise BoundaryLayerClearanceError("normalized coarse contract hash is stale")

    candidate = Path(pipeline_brep).expanduser()
    try:
        source = candidate.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceError("pipeline BREP does not exist") from error
    if (
        not source.is_file()
        or source.suffix.casefold() != ".brep"
        or _has_link_component(candidate.absolute())
        or not _is_read_only(source)
    ):
        raise BoundaryLayerClearanceError(
            "pipeline BREP must be an ordinary read-only BREP file"
        )
    contract_source = Path(str(coarse_contract.get("pipeline_brep_path", ""))).expanduser()
    try:
        contract_source = contract_source.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceError(
            "coarse contract pipeline BREP path is invalid"
        ) from error
    if contract_source != source:
        raise BoundaryLayerClearanceError(
            "pipeline BREP differs from the normalized coarse contract"
        )
    provenance = coarse_contract.get("provenance")
    if not isinstance(provenance, Mapping):
        raise BoundaryLayerClearanceError("coarse contract provenance is missing")
    expected_hash = provenance.get("pipeline_brep_sha256")
    if not _is_sha256(expected_hash) or _sha256(source) != expected_hash:
        raise BoundaryLayerClearanceError("pipeline BREP SHA-256 is stale")

    if not isinstance(marker_config, Mapping):
        raise BoundaryLayerClearanceError("marker config must be a mapping")
    clearance_contract = _validated_clearance_evidence_contract(
        coarse_contract,
        marker_config,
        source,
    )
    return source, {
        "coarse_contract_sha256": clearance_contract["sha256"],
        "required_total_thickness_m": clearance_contract[
            "required_total_thickness_m"
        ],
        "wall_physical_name": clearance_contract["wall_physical_name"],
        "wall_surface_fingerprints": clearance_contract[
            "wall_surface_fingerprints"
        ],
        "direction_probe_distance_m": clearance_contract[
            "direction_probe_distance_m"
        ],
        "coordinate_tolerance_m": clearance_contract["coordinate_tolerance_m"],
    }


def _initial_inside_distance(sample: Mapping[str, Any], sign: int) -> float:
    tests = sample.get("tests")
    if not isinstance(tests, list) or not tests:
        raise BoundaryLayerClearanceError(
            "direction sample has no bilateral inside evidence"
        )
    for index, raw in enumerate(tests):
        if not isinstance(raw, Mapping):
            raise BoundaryLayerClearanceError(
                f"direction sample test {index} is invalid"
            )
        distance = _finite(
            raw.get("epsilon_m"),
            f"direction sample test {index} distance",
            positive=True,
        )
        plus = _inside_count(raw.get("plus_inside_count"))
        minus = _inside_count(raw.get("minus_inside_count"))
        if sign == 1 and plus > 0 and minus == 0:
            return distance
        if sign == -1 and minus > 0 and plus == 0:
            return distance
    raise BoundaryLayerClearanceError(
        "direction sample does not prove its selected inward side"
    )


def _volume_diagonal(catalog: Mapping[str, Any], volume_tag: int) -> float:
    volumes = catalog.get("volumes")
    if not isinstance(volumes, list):
        raise BoundaryLayerClearanceError("geometry catalog has no volumes")
    matches = [
        record
        for record in volumes
        if isinstance(record, Mapping)
        and int(record.get("entity_tag", -1)) == volume_tag
    ]
    if len(matches) != 1:
        raise BoundaryLayerClearanceError("adjacent fluid volume is not unique")
    bounds = _vector(matches[0].get("bounding_box_m"), 6, "volume bounds")
    extents = [bounds[index + 3] - bounds[index] for index in range(3)]
    if any(value < 0.0 for value in extents):
        raise BoundaryLayerClearanceError("adjacent volume bounds are inverted")
    return _finite(
        math.sqrt(sum(value * value for value in extents)),
        "adjacent volume diagonal",
        positive=True,
    )


def audit_boundary_layer_clearance(
    *,
    gmsh_module: Any,
    pipeline_brep: str | Path,
    coarse_contract: Mapping[str, Any],
    marker_config: Mapping[str, Any],
    absolute_tolerance_m: float | None = None,
) -> dict[str, Any]:
    """Run the complete no-mesh, no-write Gmsh clearance audit.

    Runtime failures are returned as ``execution_status=FAIL`` with the full
    traceback and finalize evidence.  A completed audit whose conservative
    clearance is insufficient has ``execution_status=PASS`` but overall and
    clearance requirement status ``FAIL``; no exception is fabricated for
    that measured engineering result.
    """

    started = _utc_now()
    manifest: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL",
        "execution_status": "FAIL",
        "clearance_requirement_status": "NOT_RUN",
        "started_at_utc": started,
        "ended_at_utc": None,
        "scope": {
            "read_only_geometry_audit": True,
            "mesh_generated": False,
            "gmsh_write_called": False,
            "gui_started": False,
            "su2_called": False,
            "paraview_called": False,
            "extrusion_feasibility_claimed": False,
            "production_mesh_eligible": False,
        },
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_session": {
            "fresh_session_required": True,
            "initialize_attempted": False,
            "initialize_called": False,
            "finalize_attempted": False,
            "finalize_called": False,
            "logger_messages": [],
            "cleanup_errors": [],
        },
        "coarse_contract_sha256": None,
        "source_before": None,
        "source_after": None,
        "source_unchanged": None,
        "required_total_thickness_m": None,
        "absolute_tolerance_m": None,
        "surface_count": 0,
        "sample_count": 0,
        "surfaces": [],
        "minimum_clearance_lower_bound_m": None,
        "minimum_clearance_location": None,
        "failure_reasons": [],
        "error": None,
        "secondary_errors": [],
    }
    gmsh = gmsh_module
    source: Path | None = None
    initialized = False
    initialization_attempted = False
    logger_started = False
    primary: BaseException | None = None
    primary_traceback = ""

    def record_secondary(error: BaseException, context: str) -> None:
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        record = {
            "context": context,
            "type": type(error).__name__,
            "message": str(error),
            "traceback": rendered,
        }
        manifest["secondary_errors"].append(record)
        manifest["gmsh_session"]["cleanup_errors"].append(record)

    try:
        source, inputs = _validated_inputs(
            pipeline_brep, coarse_contract, marker_config
        )
        manifest["coarse_contract_sha256"] = inputs["coarse_contract_sha256"]
        manifest["required_total_thickness_m"] = inputs[
            "required_total_thickness_m"
        ]
        manifest["source_before"] = _snapshot(source)

        if gmsh is None:
            raise BoundaryLayerClearanceError("gmsh_module must be supplied")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_path = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_path).resolve()) if module_path else "unknown"
        )
        is_initialized = getattr(gmsh, "isInitialized", None)
        if callable(is_initialized) and bool(is_initialized()):
            raise BoundaryLayerClearanceError(
                "boundary-layer clearance audit requires a fresh Gmsh session"
            )

        initialization_attempted = True
        manifest["gmsh_session"]["initialize_attempted"] = True
        gmsh.initialize(readConfigFiles=False)
        initialized = True
        manifest["gmsh_session"]["initialize_called"] = True

        logger = getattr(gmsh, "logger", None)
        if logger is not None and callable(getattr(logger, "start", None)):
            logger.start()
            logger_started = True

        gmsh.model.add("cfdpipe_boundary_layer_clearance_audit")
        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        imported = gmsh.model.occ.importShapes(str(source))
        gmsh.model.occ.synchronize()
        if not imported:
            raise BoundaryLayerClearanceError("Gmsh imported no BREP entities")

        catalog = _collect_model(gmsh, require_normals=False)
        normalized_markers = _normalize_contract(marker_config)
        resolved = _resolve_entities(catalog, normalized_markers)
        diagnostic = resolved.get("surface_diagnostic_index")
        if not isinstance(diagnostic, Mapping):
            raise BoundaryLayerClearanceError(
                "stable surface diagnostic index is missing"
            )
        expected_fingerprints = set(inputs["wall_surface_fingerprints"])
        wall_name = str(inputs["wall_physical_name"])
        by_fingerprint: dict[str, int] = {}
        for raw_tag, raw_record in diagnostic.items():
            if not isinstance(raw_record, Mapping):
                continue
            if str(raw_record.get("physical_name", "")) != wall_name:
                continue
            fingerprint = str(raw_record.get("fingerprint_id", "")).casefold()
            tag = _positive_integer(raw_tag, "runtime wall surface tag")
            if fingerprint in by_fingerprint:
                raise BoundaryLayerClearanceError(
                    "stable wall fingerprint resolved more than once"
                )
            by_fingerprint[fingerprint] = tag
        if set(by_fingerprint) != expected_fingerprints:
            raise BoundaryLayerClearanceError(
                "fresh BREP did not resolve exactly the 48 stable wall fingerprints"
            )

        configured_probe_distance = float(inputs["direction_probe_distance_m"])
        coordinate_tolerance = float(inputs["coordinate_tolerance_m"])
        probe_distances = sorted(
            {
                min(configured_probe_distance, multiplier * coordinate_tolerance)
                for multiplier in (5.0, 50.0, 500.0)
            }
        )
        required = float(inputs["required_total_thickness_m"])
        tolerance = (
            _finite(
                absolute_tolerance_m,
                "absolute clearance tolerance",
                positive=True,
            )
            if absolute_tolerance_m is not None
            else min(coordinate_tolerance, required * 1.0e-6)
        )
        manifest["absolute_tolerance_m"] = tolerance

        surfaces: list[dict[str, Any]] = []
        all_samples: list[dict[str, Any]] = []
        for fingerprint in sorted(by_fingerprint):
            tag = by_fingerprint[fingerprint]
            direction_audit = _audit_wall_inward_direction(
                gmsh,
                catalog,
                tag,
                probe_distances_m=probe_distances,
            )
            if not isinstance(direction_audit, Mapping) or direction_audit.get(
                "status"
            ) != "PASS":
                raise BoundaryLayerClearanceError(
                    f"wall direction audit is not PASS for {fingerprint}"
                )
            adjacent_volume = _positive_integer(
                direction_audit.get("adjacent_volume_tag_audit"),
                "adjacent volume tag",
            )
            direction_sign = int(direction_audit.get("inward_height_sign", 0))
            if direction_sign not in {-1, 1}:
                raise BoundaryLayerClearanceError(
                    "wall direction audit has an invalid inward sign"
                )
            raw_samples = direction_audit.get("samples")
            if not isinstance(raw_samples, list) or len(raw_samples) != 5:
                raise BoundaryLayerClearanceError(
                    "wall direction audit must provide exactly five valid samples"
                )
            maximum_distance = 2.0 * _volume_diagonal(catalog, adjacent_volume)
            surface_samples: list[dict[str, Any]] = []
            labels: set[str] = set()
            for sample_index, raw_sample in enumerate(raw_samples):
                if not isinstance(raw_sample, Mapping):
                    raise BoundaryLayerClearanceError(
                        f"wall direction sample {sample_index} is invalid"
                    )
                label = str(raw_sample.get("label", ""))
                if not label or label in labels:
                    raise BoundaryLayerClearanceError(
                        "wall direction sample labels must be nonempty and unique"
                    )
                labels.add(label)
                sample_sign = int(raw_sample.get("resolved_height_sign", 0))
                if sample_sign != direction_sign:
                    raise BoundaryLayerClearanceError(
                        "wall direction sample sign is inconsistent"
                    )
                point = _vector(raw_sample.get("point_m"), 3, "sample point")
                normal = _vector(
                    raw_sample.get("surface_unit_normal"), 3, "surface unit normal"
                )
                inward = [sample_sign * value for value in normal]
                initial = _initial_inside_distance(raw_sample, sample_sign)
                search = probe_normal_clearance(
                    gmsh,
                    volume_tag=adjacent_volume,
                    point_m=point,
                    inward_unit_normal=inward,
                    initial_inside_distance_m=initial,
                    maximum_distance_m=maximum_distance,
                    absolute_tolerance_m=tolerance,
                )
                record = {
                    "surface_fingerprint_id": fingerprint,
                    "surface_tag_audit": tag,
                    "adjacent_volume_tag_audit": adjacent_volume,
                    "label": label,
                    "parametric_coordinates": _vector(
                        raw_sample.get("parametric_coordinates"),
                        2,
                        "sample parametric coordinates",
                    ),
                    **search,
                }
                surface_samples.append(record)
                all_samples.append(record)
            compared = audit_clearance_samples(
                surface_samples, required_total_thickness_m=required
            )
            surfaces.append(
                {
                    "surface_fingerprint_id": fingerprint,
                    "surface_tag_audit": tag,
                    "adjacent_volume_tag_audit": adjacent_volume,
                    "direction_audit": dict(direction_audit),
                    "sample_count": compared["sample_count"],
                    "minimum_clearance_lower_bound_m": compared[
                        "minimum_clearance_lower_bound_m"
                    ],
                    "minimum_conservative_margin_m": compared[
                        "minimum_conservative_margin_m"
                    ],
                    "required_total_thickness_status": compared["status"],
                    "samples": compared["samples"],
                }
            )
        global_comparison = audit_clearance_samples(
            all_samples, required_total_thickness_m=required
        )
        minimum_sample = global_comparison["samples"][
            global_comparison["minimum_clearance_sample_index"]
        ]
        manifest["surfaces"] = surfaces
        manifest["surface_count"] = len(surfaces)
        manifest["sample_count"] = global_comparison["sample_count"]
        manifest["minimum_clearance_lower_bound_m"] = global_comparison[
            "minimum_clearance_lower_bound_m"
        ]
        manifest["minimum_clearance_location"] = {
            "surface_fingerprint_id": minimum_sample["surface_fingerprint_id"],
            "surface_tag_audit": minimum_sample["surface_tag_audit"],
            "sample_label": minimum_sample["label"],
        }
        manifest["clearance_requirement_status"] = global_comparison["status"]
    except BaseException as error:
        primary = error
        primary_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    finally:
        logger = getattr(gmsh, "logger", None) if gmsh is not None else None
        if logger_started and logger is not None:
            try:
                manifest["gmsh_session"]["logger_messages"] = [
                    str(value) for value in logger.get()
                ]
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                else:
                    record_secondary(error, "gmsh.logger.get")
            try:
                logger.stop()
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                else:
                    record_secondary(error, "gmsh.logger.stop")

        should_finalize = initialized
        if not should_finalize and initialization_attempted and gmsh is not None:
            try:
                checker = getattr(gmsh, "isInitialized", None)
                should_finalize = callable(checker) and bool(checker())
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                else:
                    record_secondary(error, "gmsh.isInitialized-after-initialize-error")
        if should_finalize and gmsh is not None:
            manifest["gmsh_session"]["finalize_attempted"] = True
            try:
                gmsh.finalize()
                manifest["gmsh_session"]["finalize_called"] = True
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                else:
                    record_secondary(error, "gmsh.finalize")

    if source is not None and manifest["source_before"] is not None:
        try:
            manifest["source_after"] = _snapshot(source)
            manifest["source_unchanged"] = (
                manifest["source_after"] == manifest["source_before"]
            )
            if manifest["source_unchanged"] is not True:
                raise BoundaryLayerClearanceError(
                    "pipeline BREP changed during clearance audit"
                )
        except BaseException as error:
            if primary is None:
                primary = error
                primary_traceback = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
            else:
                record_secondary(error, "source-after-snapshot")

    manifest["ended_at_utc"] = _utc_now()
    if primary is not None:
        manifest["status"] = "FAIL"
        manifest["execution_status"] = "FAIL"
        manifest["clearance_requirement_status"] = (
            "FAIL"
            if manifest["clearance_requirement_status"] == "PASS"
            else manifest["clearance_requirement_status"]
        )
        manifest["error"] = {
            "type": type(primary).__name__,
            "message": str(primary),
            "traceback": primary_traceback,
        }
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary
        return manifest

    manifest["execution_status"] = "PASS"
    if manifest["clearance_requirement_status"] == "PASS":
        manifest["status"] = "PASS"
    else:
        manifest["status"] = "FAIL"
        manifest["failure_reasons"].append(
            "at least one conservative sampled normal clearance is below "
            "boundary_layer_design.total_thickness_m"
        )
    return manifest


def _trusted_cli_toml(
    value: str | Path,
    *,
    repository_root: Path,
    label: str,
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerClearanceError(f"{label} path must not contain '..'")
    lexical = Path(os.path.abspath(raw))
    if _has_link_component(lexical):
        raise BoundaryLayerClearanceError(f"{label} path must not use links")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceError(f"{label} does not exist") from error
    config_root = (repository_root / "config").resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.suffix.casefold() != ".toml"
        or config_root not in resolved.parents
    ):
        raise BoundaryLayerClearanceError(
            f"{label} must be an ordinary TOML file below {config_root}"
        )
    return resolved


def _load_cli_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BoundaryLayerClearanceError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise BoundaryLayerClearanceError(f"{label} must contain a TOML table")
    return value


def _prepare_cli_output_directory(
    value: str | Path, *, repository_root: Path
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance --output must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    allowed_root = (repository_root / "runs" / "mesh" / "coarse").resolve(
        strict=False
    )
    if repository_root not in allowed_root.parents:
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance output root escapes the repository"
        )
    if _has_link_component(allowed_root):
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance output root must not use links"
        )
    allowed_root.mkdir(parents=True, exist_ok=True)
    if _has_link_component(allowed_root):
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance output root must not use links"
        )
    resolved_root = allowed_root.resolve(strict=True)
    resolved = lexical.resolve(strict=False)
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise BoundaryLayerClearanceError(
            f"boundary-layer clearance output must be a new child of {resolved_root}"
        )
    if (
        lexical.exists()
        or lexical.is_symlink()
        or resolved.exists()
        or _has_link_component(lexical.parent)
    ):
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance output must be new and must not use links"
        )
    lexical.mkdir(parents=True, exist_ok=False)
    if lexical.resolve(strict=True) != resolved or _has_link_component(lexical):
        raise BoundaryLayerClearanceError(
            "boundary-layer clearance output changed during creation"
        )
    return lexical


def _command_failure_manifest(
    error: BaseException,
    *,
    started_at_utc: str,
    config_path: Path,
    markers_path: Path,
    output_directory: Path,
) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "status": "FAIL",
        "execution_status": "FAIL",
        "clearance_requirement_status": "NOT_RUN",
        "started_at_utc": started_at_utc,
        "ended_at_utc": _utc_now(),
        "scope": {
            "read_only_geometry_audit": True,
            "mesh_generated": False,
            "gmsh_write_called": False,
            "gui_started": False,
            "su2_called": False,
            "paraview_called": False,
            "extrusion_feasibility_claimed": False,
            "production_mesh_eligible": False,
        },
        "command_inputs": {
            "config_path": str(config_path),
            "markers_path": str(markers_path),
            "output_directory": str(output_directory),
        },
        "gmsh_session": {
            "initialize_attempted": False,
            "initialize_called": False,
            "finalize_attempted": False,
            "finalize_called": False,
        },
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        },
    }


def run_boundary_layer_clearance_command(
    *,
    config_path: str | Path,
    markers_path: str | Path,
    output_directory: str | Path,
    repository_root: str | Path,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Run the reproducible CLI audit and publish one strict JSON report.

    The optional ``gmsh_module`` argument exists only for dependency-injected
    tests.  Normal command execution performs a regular, delayed
    ``import gmsh`` after every path and configuration preflight has passed.
    """

    started = _utc_now()
    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceError("repository root does not exist") from error
    if not root.is_dir():
        raise BoundaryLayerClearanceError("repository root is not a directory")
    output = _prepare_cli_output_directory(output_directory, repository_root=root)
    config = Path(os.path.abspath(Path(config_path).expanduser()))
    markers = Path(os.path.abspath(Path(markers_path).expanduser()))

    try:
        config = _trusted_cli_toml(
            config_path,
            repository_root=root,
            label="coarse mesh config",
        )
        markers = _trusted_cli_toml(
            markers_path,
            repository_root=root,
            label="marker config",
        )
        config_document = _load_cli_toml(config, "coarse mesh config")
        marker_document = _load_cli_toml(markers, "marker config")

        from .coarse_mesh import normalize_coarse_mesh_config

        contract = normalize_coarse_mesh_config(
            config_document, repository_root=root
        )
        if gmsh_module is None:
            import gmsh as imported_gmsh

            gmsh_module = imported_gmsh
        manifest = audit_boundary_layer_clearance(
            gmsh_module=gmsh_module,
            pipeline_brep=contract.get("pipeline_brep_path", ""),
            coarse_contract=contract,
            marker_config=marker_document,
        )
        if not isinstance(manifest, dict):
            raise BoundaryLayerClearanceError(
                "boundary-layer clearance audit returned an invalid manifest"
            )
        manifest["command_inputs"] = {
            "config_path": str(config),
            "config_sha256": _sha256(config),
            "markers_path": str(markers),
            "markers_sha256": _sha256(markers),
            "output_directory": str(output),
        }
    except Exception as error:
        manifest = _command_failure_manifest(
            error,
            started_at_utc=started,
            config_path=config,
            markers_path=markers,
            output_directory=output,
        )

    report_path = output / "boundary_layer_clearance.json"
    manifest["report_path"] = str(report_path)
    try:
        rendered = json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as error:
        manifest = _command_failure_manifest(
            error,
            started_at_utc=started,
            config_path=config,
            markers_path=markers,
            output_directory=output,
        )
        manifest["report_path"] = str(report_path)
        rendered = json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
    return manifest


__all__ = [
    "BoundaryLayerClearanceError",
    "audit_boundary_layer_clearance",
    "audit_clearance_samples",
    "probe_clearance",
    "probe_normal_clearance",
    "run_boundary_layer_clearance_command",
]
