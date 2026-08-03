"""Clearance-aware local prism-stack planning without launching Gmsh.

The clearance audit intentionally reports a uniform-stack failure when even
one of its 240 wall-normal samples cannot accommodate the global design
height.  This module consumes that completed audit as evidence and derives a
per-surface growth ratio while preserving the hash-bound first height and
layer count.  It never reduces the layer count or increases the first height.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tomllib
import traceback
from typing import Any, Mapping, Sequence


_SCHEMA = "cfdpipe.boundary_layer_local_schedule.v1"
_COARSE_SCHEMA = "cfdpipe.coarse_mesh.v1"
_CLEARANCE_SCHEMA = "cfdpipe.boundary_layer_clearance.v1"
_CLEARANCE_EVIDENCE_CONTRACT_SCHEMA = (
    "cfdpipe.boundary_layer_clearance_evidence_contract.v1"
)
_WALL_SURFACE_COUNT = 48
_SAMPLES_PER_SURFACE = 5
_TOTAL_SAMPLE_COUNT = _WALL_SURFACE_COUNT * _SAMPLES_PER_SURFACE
_SHA256_LENGTH = 64


class BoundaryLayerLocalScheduleError(RuntimeError):
    """Raised when local scheduling evidence is invalid or ambiguous."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerLocalScheduleError(f"{label} must be finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerLocalScheduleError(
            f"{label} must be finite numeric data"
        ) from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "finite and positive" if positive else "finite"
        raise BoundaryLayerLocalScheduleError(f"{label} must be {qualifier}")
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerLocalScheduleError(f"{label} must be a positive integer")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise BoundaryLayerLocalScheduleError(
            "local boundary-layer evidence is not canonically serializable"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _assert_all_numbers_finite(value: object, label: str = "clearance report") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BoundaryLayerLocalScheduleError(f"{label} contains non-finite data")
        return
    if isinstance(value, (str, bytes, int, bool)) or value is None:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BoundaryLayerLocalScheduleError(
                    f"{label} contains a non-string JSON key"
                )
            _assert_all_numbers_finite(item, f"{label}.{key}")
        return
    if isinstance(value, Sequence):
        for index, item in enumerate(value):
            _assert_all_numbers_finite(item, f"{label}[{index}]")
        return
    raise BoundaryLayerLocalScheduleError(
        f"{label} contains non-JSON data of type {type(value).__name__}"
    )


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoundaryLayerLocalScheduleError(
                f"clearance report contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise BoundaryLayerLocalScheduleError(
        f"clearance report contains non-finite JSON constant {value}"
    )


def _load_strict_json(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        result = json.loads(
            decoded,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except BoundaryLayerLocalScheduleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoundaryLayerLocalScheduleError(
            f"clearance report is not valid UTF-8 JSON: {error}"
        ) from error
    if not isinstance(result, dict):
        raise BoundaryLayerLocalScheduleError(
            "clearance report JSON root must be an object"
        )
    _assert_all_numbers_finite(result)
    return result


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    junction = getattr(path, "is_junction", None)
    return (
        path.is_symlink()
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        or (bool(junction()) if callable(junction) else False)
    )


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


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
    }


def _same_float(
    left: object, right: object, label: str, *, positive: bool = True
) -> float:
    a = _finite(left, label, positive=positive)
    b = _finite(right, f"expected {label}", positive=positive)
    if not math.isclose(a, b, rel_tol=1.0e-11, abs_tol=1.0e-14):
        raise BoundaryLayerLocalScheduleError(f"{label} is internally inconsistent")
    return a


def _stack_total(first_height_m: float, layer_count: int, growth_ratio: float) -> float:
    # Direct summation is stable at r ~= 1 and the configured count is small.
    total = math.fsum(first_height_m * growth_ratio**index for index in range(layer_count))
    if not math.isfinite(total) or total <= 0.0:
        raise BoundaryLayerLocalScheduleError("boundary-layer stack total is invalid")
    return total


def solve_growth_ratio(
    *,
    first_layer_height_m: float,
    layer_count: int,
    target_total_thickness_m: float,
    maximum_growth_ratio: float,
) -> float:
    """Solve the unique geometric growth ratio in ``[1, maximum]``.

    For a fixed positive first height and at least two layers, the geometric
    sum is strictly increasing in the ratio.  Bisection therefore provides a
    deterministic solution and behaves well close to unity.
    """

    first = _finite(first_layer_height_m, "first layer height", positive=True)
    count = _positive_integer(layer_count, "layer count")
    target = _finite(target_total_thickness_m, "target total thickness", positive=True)
    maximum = _finite(maximum_growth_ratio, "maximum growth ratio", positive=True)
    if count < 15:
        raise BoundaryLayerLocalScheduleError("local stack must retain at least 15 layers")
    if maximum < 1.0:
        raise BoundaryLayerLocalScheduleError("maximum growth ratio must be at least one")

    minimum_total = _stack_total(first, count, 1.0)
    maximum_total = _stack_total(first, count, maximum)
    tolerance = max(1.0e-15, target * 1.0e-12)
    if target < minimum_total - tolerance:
        raise BoundaryLayerLocalScheduleError(
            "clearance budget cannot contain the fixed first height and layer count"
        )
    if target > maximum_total + tolerance:
        raise BoundaryLayerLocalScheduleError(
            "target thickness exceeds the configured maximum growth stack"
        )
    if abs(target - minimum_total) <= tolerance:
        return 1.0
    if abs(target - maximum_total) <= tolerance:
        return maximum

    lower = 1.0
    upper = maximum
    for _ in range(160):
        midpoint = lower + 0.5 * (upper - lower)
        if _stack_total(first, count, midpoint) < target:
            lower = midpoint
        else:
            upper = midpoint
    result = lower + 0.5 * (upper - lower)
    reconstructed = _stack_total(first, count, result)
    if not math.isclose(reconstructed, target, rel_tol=1.0e-11, abs_tol=1.0e-14):
        raise BoundaryLayerLocalScheduleError(
            "local growth-ratio bisection did not reconstruct the target thickness"
        )
    return result


def _validated_contract(coarse_contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(coarse_contract, Mapping):
        raise BoundaryLayerLocalScheduleError("coarse contract must be a mapping")
    if coarse_contract.get("schema") != _COARSE_SCHEMA:
        raise BoundaryLayerLocalScheduleError("coarse contract schema is invalid")
    configured_hash = coarse_contract.get("normalized_config_sha256")
    unsigned = dict(coarse_contract)
    unsigned.pop("normalized_config_sha256", None)
    if not _is_sha256(configured_hash) or _canonical_sha256(unsigned) != configured_hash:
        raise BoundaryLayerLocalScheduleError("normalized coarse contract hash is stale")
    clearance_contract_hash = coarse_contract.get(
        "clearance_evidence_contract_sha256"
    )
    clearance_evidence_contract = coarse_contract.get(
        "clearance_evidence_contract"
    )
    if (
        not _is_sha256(clearance_contract_hash)
        or not isinstance(clearance_evidence_contract, Mapping)
        or clearance_evidence_contract.get("schema")
        != _CLEARANCE_EVIDENCE_CONTRACT_SCHEMA
        or _canonical_sha256(clearance_evidence_contract)
        != clearance_contract_hash
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance evidence sub-contract is missing or stale"
        )

    design = coarse_contract.get("boundary_layer_design")
    fingerprints = coarse_contract.get("wall_surface_fingerprints")
    provenance = coarse_contract.get("provenance")
    if (
        not isinstance(design, Mapping)
        or design.get("status") != "PASS"
        or not isinstance(fingerprints, list)
        or not isinstance(provenance, Mapping)
    ):
        raise BoundaryLayerLocalScheduleError(
            "coarse boundary-layer scheduling inputs are incomplete"
        )
    normalized_fingerprints = [str(value).casefold() for value in fingerprints]
    if (
        len(normalized_fingerprints) != _WALL_SURFACE_COUNT
        or len(set(normalized_fingerprints)) != _WALL_SURFACE_COUNT
        or any(not _is_sha256(value) for value in normalized_fingerprints)
    ):
        raise BoundaryLayerLocalScheduleError(
            "coarse contract must contain 48 unique wall fingerprints"
        )
    first = _finite(design.get("first_layer_height_m"), "first layer height", positive=True)
    count = _positive_integer(design.get("layer_count"), "layer count")
    project_minimum_count = _positive_integer(
        design.get("project_minimum_layer_count"), "project minimum layer count"
    )
    if project_minimum_count < 15 or count < project_minimum_count:
        raise BoundaryLayerLocalScheduleError("coarse design has fewer than 15 layers")
    growth = _finite(design.get("growth_ratio"), "project growth ratio", positive=True)
    if growth < 1.0:
        raise BoundaryLayerLocalScheduleError("project growth ratio is below one")
    global_total = _finite(
        design.get("total_thickness_m"), "global design thickness", positive=True
    )
    reconstructed_global = _stack_total(first, count, growth)
    if not math.isclose(
        reconstructed_global, global_total, rel_tol=1.0e-11, abs_tol=1.0e-14
    ):
        raise BoundaryLayerLocalScheduleError(
            "global design thickness does not match its first height/count/growth"
        )
    maximum_core_ratio = _finite(
        design.get("maximum_core_to_last_layer_ratio"),
        "maximum core-to-last-layer ratio",
        positive=True,
    )
    fraction = _finite(
        coarse_contract.get("boundary_layer_maximum_clearance_fraction"),
        "maximum clearance fraction",
        positive=True,
    )
    if fraction >= 0.5:
        raise BoundaryLayerLocalScheduleError(
            "maximum clearance fraction must be below one half"
        )

    source = Path(str(coarse_contract.get("pipeline_brep_path", ""))).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "coarse contract pipeline BREP does not exist"
        ) from error
    source_hash = provenance.get("pipeline_brep_sha256")
    if (
        not source.is_file()
        or not _is_sha256(source_hash)
        or _sha256(source) != source_hash
        or _is_link_like(source)
        or not _is_read_only(source)
    ):
        raise BoundaryLayerLocalScheduleError(
            "coarse contract pipeline BREP evidence is stale"
        )

    source_identity = clearance_evidence_contract.get("source_geometry")
    marker_identity = clearance_evidence_contract.get("marker_config")
    wall_identity = clearance_evidence_contract.get("wall_selection")
    requirement = clearance_evidence_contract.get("clearance_requirement")
    policy = clearance_evidence_contract.get("audit_policy")
    scope = clearance_evidence_contract.get("scope")
    base = coarse_contract.get("base_smoke_contract")
    if (
        set(clearance_evidence_contract) != {
            "schema",
            "source_geometry",
            "marker_config",
            "wall_selection",
            "clearance_requirement",
            "audit_policy",
            "scope",
        }
        or not isinstance(source_identity, Mapping)
        or not isinstance(marker_identity, Mapping)
        or not isinstance(wall_identity, Mapping)
        or not isinstance(requirement, Mapping)
        or not isinstance(policy, Mapping)
        or not isinstance(scope, Mapping)
        or not isinstance(base, Mapping)
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance evidence sub-contract is incomplete"
        )
    try:
        clearance_source = Path(str(source_identity.get("path", ""))).resolve(
            strict=True
        )
        marker_path = Path(str(marker_identity.get("path", ""))).resolve(
            strict=True
        )
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "clearance evidence source path is stale"
        ) from error
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
    marker_hash = coarse_contract.get("provenance", {}).get("markers_sha256")
    if (
        set(source_identity) != {
            "path",
            "sha256",
            "required_suffix",
            "read_only_required",
            "links_allowed",
        }
        or clearance_source != source
        or source_identity.get("sha256") != source_hash
        or source_identity.get("required_suffix") != ".brep"
        or source_identity.get("read_only_required") is not True
        or source_identity.get("links_allowed") is not False
        or set(marker_identity) != {
            "path",
            "sha256",
            "pipeline_geometry_sha256",
            "coordinate_absolute_tolerance_m",
        }
        or not marker_path.is_file()
        or marker_path.suffix.casefold() != ".toml"
        or _is_link_like(marker_path)
        or not _is_sha256(marker_hash)
        or marker_identity.get("sha256") != marker_hash
        or _sha256(marker_path) != marker_hash
        or marker_identity.get("pipeline_geometry_sha256") != source_hash
        or set(wall_identity) != {
            "physical_name",
            "stable_surface_fingerprint_count",
            "stable_surface_fingerprints",
        }
        or wall_identity.get("physical_name") != base.get("wall_physical_name")
        or wall_identity.get("stable_surface_fingerprint_count") != 48
        or wall_identity.get("stable_surface_fingerprints")
        != sorted(normalized_fingerprints)
        or set(requirement) != {"required_total_thickness_m"}
        or requirement.get("required_total_thickness_m") != global_total
        or dict(policy) != expected_policy
        or dict(scope) != expected_scope
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance evidence sub-contract differs from current geometry inputs"
        )
    _finite(
        marker_identity.get("coordinate_absolute_tolerance_m"),
        "clearance marker coordinate tolerance",
        positive=True,
    )
    return {
        "normalized_config_sha256": configured_hash,
        "clearance_evidence_contract_sha256": clearance_contract_hash,
        "fingerprints": sorted(normalized_fingerprints),
        "first_layer_height_m": first,
        "layer_count": count,
        "project_minimum_layer_count": project_minimum_count,
        "maximum_growth_ratio": growth,
        "global_total_thickness_m": global_total,
        "maximum_core_to_last_layer_ratio": maximum_core_ratio,
        "maximum_clearance_fraction": fraction,
        "pipeline_brep_path": source,
        "pipeline_brep_sha256": source_hash,
        "clearance_evidence_contract": dict(clearance_evidence_contract),
    }


def _validate_clearance_report(
    report: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    allowed_contract_hashes: set[str] | None = None,
) -> list[dict[str, Any]]:
    if report.get("schema") != _CLEARANCE_SCHEMA:
        raise BoundaryLayerLocalScheduleError("clearance report schema is invalid")
    if report.get("execution_status") != "PASS":
        raise BoundaryLayerLocalScheduleError("clearance audit execution is not PASS")
    if report.get("status") not in {"PASS", "FAIL"} or report.get(
        "clearance_requirement_status"
    ) not in {"PASS", "FAIL"}:
        raise BoundaryLayerLocalScheduleError(
            "clearance report engineering status is invalid"
        )
    report_contract_hash = report.get("coarse_contract_sha256")
    accepted_hashes = (
        {
            contract["normalized_config_sha256"],
            contract["clearance_evidence_contract_sha256"],
        }
        if allowed_contract_hashes is None
        else set(allowed_contract_hashes)
    )
    if (
        not accepted_hashes
        or any(not _is_sha256(value) for value in accepted_hashes)
        or report_contract_hash not in accepted_hashes
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report is not bound to the expected audit contract"
        )
    _same_float(
        report.get("required_total_thickness_m"),
        contract["global_total_thickness_m"],
        "clearance report required total thickness",
    )

    session = report.get("gmsh_session")
    if (
        not isinstance(session, Mapping)
        or session.get("initialize_called") is not True
        or session.get("finalize_attempted") is not True
        or session.get("finalize_called") is not True
        or session.get("cleanup_errors") != []
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report lacks clean Gmsh initialize/finalize evidence"
        )
    if report.get("error") is not None or report.get("secondary_errors") != []:
        raise BoundaryLayerLocalScheduleError(
            "clearance report contains execution or cleanup errors"
        )
    scope = report.get("scope")
    required_scope = {
        "read_only_geometry_audit": True,
        "mesh_generated": False,
        "gmsh_write_called": False,
        "gui_started": False,
        "su2_called": False,
        "paraview_called": False,
        "extrusion_feasibility_claimed": False,
        "production_mesh_eligible": False,
    }
    if not isinstance(scope, Mapping) or any(
        scope.get(key) is not value for key, value in required_scope.items()
    ):
        raise BoundaryLayerLocalScheduleError("clearance report scope is unsafe")

    source_before = report.get("source_before")
    source_after = report.get("source_after")
    if (
        not isinstance(source_before, Mapping)
        or not isinstance(source_after, Mapping)
        or source_before != source_after
        or report.get("source_unchanged") is not True
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report source was not preserved unchanged"
        )
    try:
        report_source = Path(str(source_before.get("path", ""))).resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "clearance report source path no longer exists"
        ) from error
    if (
        report_source != contract["pipeline_brep_path"]
        or source_before.get("sha256") != contract["pipeline_brep_sha256"]
        or source_before.get("read_only") is not True
        or _sha256(report_source) != contract["pipeline_brep_sha256"]
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report source path/SHA-256 is stale"
        )

    surfaces = report.get("surfaces")
    if (
        not isinstance(surfaces, list)
        or report.get("surface_count") != _WALL_SURFACE_COUNT
        or len(surfaces) != _WALL_SURFACE_COUNT
        or report.get("sample_count") != _TOTAL_SAMPLE_COUNT
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report must contain exactly 48 surfaces and 240 samples"
        )

    required = float(contract["global_total_thickness_m"])
    expected_fingerprints = set(contract["fingerprints"])
    normalized: list[dict[str, Any]] = []
    observed_fingerprints: set[str] = set()
    all_minima: list[float] = []
    uniform_pass = True
    for surface_index, raw_surface in enumerate(surfaces):
        if not isinstance(raw_surface, Mapping):
            raise BoundaryLayerLocalScheduleError(
                f"clearance surface {surface_index} is not an object"
            )
        fingerprint = str(raw_surface.get("surface_fingerprint_id", "")).casefold()
        if (
            fingerprint not in expected_fingerprints
            or fingerprint in observed_fingerprints
        ):
            raise BoundaryLayerLocalScheduleError(
                "clearance report has missing, duplicate, or unknown wall fingerprints"
            )
        observed_fingerprints.add(fingerprint)
        samples = raw_surface.get("samples")
        if (
            not isinstance(samples, list)
            or raw_surface.get("sample_count") != _SAMPLES_PER_SURFACE
            or len(samples) != _SAMPLES_PER_SURFACE
        ):
            raise BoundaryLayerLocalScheduleError(
                f"wall fingerprint {fingerprint} does not contain five samples"
            )
        labels: set[str] = set()
        lower_bounds: list[float] = []
        for sample_index, sample in enumerate(samples):
            if not isinstance(sample, Mapping):
                raise BoundaryLayerLocalScheduleError(
                    f"clearance sample {surface_index}:{sample_index} is invalid"
                )
            if str(sample.get("surface_fingerprint_id", "")).casefold() != fingerprint:
                raise BoundaryLayerLocalScheduleError(
                    "clearance sample fingerprint differs from its surface"
                )
            label = str(sample.get("label", "")).strip()
            if not label or label in labels:
                raise BoundaryLayerLocalScheduleError(
                    "clearance sample labels must be nonempty and unique per surface"
                )
            labels.add(label)
            lower = _finite(
                sample.get("clearance_lower_bound_m"),
                f"clearance lower bound {surface_index}:{sample_index}",
                positive=True,
            )
            upper = _finite(
                sample.get("clearance_upper_bound_m"),
                f"clearance upper bound {surface_index}:{sample_index}",
                positive=True,
            )
            if lower > upper:
                raise BoundaryLayerLocalScheduleError(
                    "clearance sample bracket is inverted"
                )
            _same_float(
                sample.get("required_total_thickness_m"),
                required,
                "sample required total thickness",
            )
            _same_float(
                sample.get("conservative_clearance_margin_m"),
                lower - required,
                "sample conservative clearance margin",
                positive=False,
            )
            if sample.get("required_total_thickness_clear") is not (lower >= required):
                raise BoundaryLayerLocalScheduleError(
                    "clearance sample PASS/FAIL flag is inconsistent"
                )
            lower_bounds.append(lower)
        minimum = min(lower_bounds)
        _same_float(
            raw_surface.get("minimum_clearance_lower_bound_m"),
            minimum,
            "surface minimum clearance",
        )
        _same_float(
            raw_surface.get("minimum_conservative_margin_m"),
            minimum - required,
            "surface conservative clearance margin",
            positive=False,
        )
        surface_uniform_pass = minimum >= required
        if raw_surface.get("required_total_thickness_status") != (
            "PASS" if surface_uniform_pass else "FAIL"
        ):
            raise BoundaryLayerLocalScheduleError(
                "surface clearance requirement status is inconsistent"
            )
        uniform_pass = uniform_pass and surface_uniform_pass
        all_minima.append(minimum)
        normalized.append(
            {
                "surface_fingerprint_id": fingerprint,
                "minimum_clearance_lower_bound_m": minimum,
                "sample_clearance_lower_bounds_m": lower_bounds,
                "surface_clearance_evidence_sha256": _canonical_sha256(raw_surface),
            }
        )

    if observed_fingerprints != expected_fingerprints:
        raise BoundaryLayerLocalScheduleError(
            "clearance report does not cover the normalized wall fingerprints"
        )
    global_minimum = min(all_minima)
    _same_float(
        report.get("minimum_clearance_lower_bound_m"),
        global_minimum,
        "global minimum clearance",
    )
    expected_requirement = "PASS" if uniform_pass else "FAIL"
    if (
        report.get("clearance_requirement_status") != expected_requirement
        or report.get("status") != expected_requirement
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report overall engineering status is inconsistent"
        )
    return sorted(normalized, key=lambda value: value["surface_fingerprint_id"])


def plan_local_boundary_layer_schedules(
    *,
    coarse_contract: Mapping[str, Any],
    clearance_report_path: str | os.PathLike[str],
    clearance_report_sha256: str,
) -> dict[str, Any]:
    """Return strict JSON-ready schedules for all 48 stable wall surfaces.

    ``clearance_report_path`` and ``clearance_report_sha256`` are mandatory so
    the caller, not a directory search, chooses and binds the evidence file.
    The report may have overall ``FAIL`` solely because the uniform global
    stack is too thick; its execution and lifecycle evidence must still PASS.
    """

    contract = _validated_contract(coarse_contract)
    expected_report_hash = str(clearance_report_sha256).casefold()
    if not _is_sha256(expected_report_hash):
        raise BoundaryLayerLocalScheduleError(
            "caller-supplied clearance report SHA-256 is invalid"
        )
    candidate = Path(clearance_report_path).expanduser()
    try:
        if candidate.exists() and _is_link_like(candidate):
            raise BoundaryLayerLocalScheduleError(
                "clearance report path must not be a symbolic link or junction"
            )
        report_path = candidate.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "caller-supplied clearance report path does not exist"
        ) from error
    if not report_path.is_file():
        raise BoundaryLayerLocalScheduleError("clearance report path is not a file")

    before = _snapshot(report_path)
    if before["sha256"] != expected_report_hash:
        raise BoundaryLayerLocalScheduleError(
            "caller-supplied clearance report SHA-256 does not match the file"
        )
    payload = report_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_report_hash:
        raise BoundaryLayerLocalScheduleError(
            "clearance report changed while it was being read"
        )
    report = _load_strict_json(payload)
    surfaces = _validate_clearance_report(report, contract)
    after = _snapshot(report_path)
    if after != before:
        raise BoundaryLayerLocalScheduleError(
            "clearance report changed during local schedule planning"
        )

    first = float(contract["first_layer_height_m"])
    count = int(contract["layer_count"])
    maximum_growth = float(contract["maximum_growth_ratio"])
    global_total = float(contract["global_total_thickness_m"])
    fraction = float(contract["maximum_clearance_fraction"])
    maximum_core_ratio = float(contract["maximum_core_to_last_layer_ratio"])
    minimum_fixed_stack = _stack_total(first, count, 1.0)

    schedules: list[dict[str, Any]] = []
    failures: list[str] = []
    for surface in surfaces:
        fingerprint = str(surface["surface_fingerprint_id"])
        clearance = float(surface["minimum_clearance_lower_bound_m"])
        clearance_budget = clearance * fraction
        target = min(global_total, clearance_budget)
        base_record: dict[str, Any] = {
            "surface_fingerprint_id": fingerprint,
            "status": "FAIL",
            "layer_count": count,
            "first_layer_height_m": first,
            "maximum_growth_ratio": maximum_growth,
            "global_design_total_thickness_m": global_total,
            "minimum_clearance_lower_bound_m": clearance,
            "maximum_clearance_fraction": fraction,
            "clearance_limited_target_total_thickness_m": target,
            "minimum_fixed_stack_total_thickness_m": minimum_fixed_stack,
            "surface_clearance_evidence_sha256": surface[
                "surface_clearance_evidence_sha256"
            ],
            "clearance_report_sha256": expected_report_hash,
            "coarse_contract_sha256": contract["normalized_config_sha256"],
            "failure_reason": None,
        }
        if target < minimum_fixed_stack and not math.isclose(
            target,
            minimum_fixed_stack,
            rel_tol=1.0e-12,
            abs_tol=1.0e-15,
        ):
            reason = (
                f"{fingerprint}: clearance fraction budget is below the fixed "
                f"{count}-layer stack minimum implied by the normalized layer count"
            )
            base_record["failure_reason"] = reason
            base_record.update(
                {
                    "local_growth_ratio": None,
                    "total_thickness_m": None,
                    "last_layer_height_m": None,
                    "recommended_local_core_max_m": None,
                    "clearance_margin_m": None,
                    "two_sided_core_clearance_margin_m": None,
                    "schedule_evidence_sha256": _canonical_sha256(base_record),
                }
            )
            failures.append(reason)
            schedules.append(base_record)
            continue

        growth = solve_growth_ratio(
            first_layer_height_m=first,
            layer_count=count,
            target_total_thickness_m=target,
            maximum_growth_ratio=maximum_growth,
        )
        total = _stack_total(first, count, growth)
        last = first * growth ** (count - 1)
        recommended_core = last * maximum_core_ratio
        margin = clearance - total
        two_sided_margin = clearance - 2.0 * total
        audit_values = (growth, total, last, recommended_core, margin, two_sided_margin)
        if any(not math.isfinite(value) for value in audit_values):
            raise BoundaryLayerLocalScheduleError(
                "local boundary-layer schedule contains non-finite values"
            )
        if (
            not 1.0 <= growth <= maximum_growth
            or not math.isclose(total, target, rel_tol=1.0e-11, abs_tol=1.0e-14)
            or margin <= 0.0
            or two_sided_margin <= 0.0
        ):
            raise BoundaryLayerLocalScheduleError(
                "local boundary-layer schedule violates its clearance invariant"
            )
        base_record.update(
            {
                "status": "PASS",
                "local_growth_ratio": growth,
                "total_thickness_m": total,
                "last_layer_height_m": last,
                "maximum_core_to_last_layer_ratio": maximum_core_ratio,
                "recommended_local_core_max_m": recommended_core,
                "clearance_margin_m": margin,
                "two_sided_core_clearance_margin_m": two_sided_margin,
            }
        )
        base_record["schedule_evidence_sha256"] = _canonical_sha256(base_record)
        schedules.append(base_record)

    result: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL" if failures else "PASS",
        "planning_only": True,
        "extrusion_authorized": False,
        "extrusion_feasibility_claimed": False,
        "requires_builder_integration_and_mesh_quality_gate": True,
        "mesh_generated": False,
        "su2_called": False,
        "paraview_called": False,
        "production_mesh_eligible": False,
        "coarse_contract_sha256": contract["normalized_config_sha256"],
        "clearance_evidence_contract_sha256": contract[
            "clearance_evidence_contract_sha256"
        ],
        "clearance_report": {
            "path": str(report_path),
            "sha256": expected_report_hash,
            "size_bytes": before["size_bytes"],
            "unchanged_during_planning": after == before,
            "execution_status": report["execution_status"],
            "coarse_contract_sha256": report["coarse_contract_sha256"],
            "contract_binding": (
                "current_contract"
                if report["coarse_contract_sha256"]
                == contract["normalized_config_sha256"]
                else "preserved_pre_local_policy_clearance_contract"
            ),
            "uniform_clearance_requirement_status": report[
                "clearance_requirement_status"
            ],
        },
        "source_geometry": {
            "path": str(contract["pipeline_brep_path"]),
            "sha256": contract["pipeline_brep_sha256"],
            "unchanged_in_clearance_audit": report["source_unchanged"],
            "gmsh_finalize_called": report["gmsh_session"]["finalize_called"],
        },
        "policy": {
            "first_layer_height_m": first,
            "layer_count": count,
            "minimum_layer_count": contract["project_minimum_layer_count"],
            "maximum_growth_ratio": maximum_growth,
            "global_design_total_thickness_m": global_total,
            "maximum_clearance_fraction": fraction,
            "maximum_core_to_last_layer_ratio": maximum_core_ratio,
            "first_layer_height_reduced": False,
            "layer_count_reduced": False,
        },
        "surface_count": len(schedules),
        "sample_count": _TOTAL_SAMPLE_COUNT,
        "pass_surface_count": sum(item["status"] == "PASS" for item in schedules),
        "fail_surface_count": sum(item["status"] == "FAIL" for item in schedules),
        "schedules": schedules,
        "failure_reasons": failures,
    }
    _assert_all_numbers_finite(result, "local schedule plan")
    # This is also the final strict-JSON serializability gate.
    json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return result


def _prepare_command_output(
    value: str | os.PathLike[str], *, repository_root: Path
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan --output must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    allowed_root = repository_root / "runs" / "mesh" / "coarse"
    if _has_link_component(allowed_root):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan output root must not use links"
        )
    allowed_root.mkdir(parents=True, exist_ok=True)
    if _has_link_component(allowed_root):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan output root must not use links"
        )
    resolved_root = allowed_root.resolve(strict=True)
    resolved = lexical.resolve(strict=False)
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise BoundaryLayerLocalScheduleError(
            f"boundary-layer local plan output must be a new child of {resolved_root}"
        )
    if (
        lexical.exists()
        or lexical.is_symlink()
        or resolved.exists()
        or _has_link_component(lexical.parent)
    ):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan output must be new and must not use links"
        )
    lexical.mkdir(parents=True, exist_ok=False)
    if lexical.resolve(strict=True) != resolved or _has_link_component(lexical):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan output changed during creation"
        )
    return lexical


def _trusted_command_config(value: str | os.PathLike[str], root: Path) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan --config must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    if _has_link_component(lexical):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan config path must not use links"
        )
    try:
        resolved = lexical.resolve(strict=True)
        expected = (root / "config" / "coarse_mesh.toml").resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan config does not exist"
        ) from error
    if resolved != expected or not resolved.is_file():
        raise BoundaryLayerLocalScheduleError(
            f"boundary-layer local plan requires exactly {expected}"
        )
    return resolved


def _trusted_command_clearance_report(
    value: str | os.PathLike[str], root: Path
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan --clearance-report must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    if _has_link_component(lexical):
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan clearance report path must not use links"
        )
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "boundary-layer local plan clearance report does not exist"
        ) from error
    allowed_root = (root / "runs" / "mesh" / "coarse").resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.suffix.casefold() != ".json"
        or allowed_root not in resolved.parents
    ):
        raise BoundaryLayerLocalScheduleError(
            "clearance report must be an explicit JSON file below "
            f"{allowed_root}"
        )
    return resolved


def _command_failure_report(
    error: BaseException,
    *,
    started_at_utc: str,
    config_path: Path,
    clearance_report_path: Path,
    clearance_report_sha256: str,
    output_directory: Path,
) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "status": "FAIL",
        "planning_only": True,
        "extrusion_authorized": False,
        "extrusion_feasibility_claimed": False,
        "mesh_generated": False,
        "su2_called": False,
        "paraview_called": False,
        "production_mesh_eligible": False,
        "started_at_utc": started_at_utc,
        "ended_at_utc": _utc_now(),
        "command_inputs": {
            "config_path": str(config_path),
            "clearance_report_path": str(clearance_report_path),
            "clearance_report_sha256": str(clearance_report_sha256).casefold(),
            "output_directory": str(output_directory),
        },
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        },
    }


def run_boundary_layer_local_schedule_command(
    *,
    config_path: str | os.PathLike[str],
    clearance_report_path: str | os.PathLike[str],
    clearance_report_sha256: str,
    output_directory: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Normalize, plan, and publish one new strict JSON evidence file.

    This command is deliberately pure Python: it imports neither Gmsh nor
    ParaView, constructs no command runner, and launches no external program.
    """

    started = _utc_now()
    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerLocalScheduleError(
            "repository root does not exist"
        ) from error
    if not root.is_dir():
        raise BoundaryLayerLocalScheduleError("repository root is not a directory")
    output = _prepare_command_output(output_directory, repository_root=root)
    config = Path(os.path.abspath(Path(config_path).expanduser()))
    clearance = Path(os.path.abspath(Path(clearance_report_path).expanduser()))

    try:
        config = _trusted_command_config(config_path, root)
        clearance = _trusted_command_clearance_report(clearance_report_path, root)
        try:
            with config.open("rb") as stream:
                config_document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise BoundaryLayerLocalScheduleError(
                f"cannot read coarse mesh config: {error}"
            ) from error

        from .coarse_mesh import normalize_coarse_mesh_config

        contract = normalize_coarse_mesh_config(
            config_document, repository_root=root
        )
        manifest = plan_local_boundary_layer_schedules(
            coarse_contract=contract,
            clearance_report_path=clearance,
            clearance_report_sha256=clearance_report_sha256,
        )
        if not isinstance(manifest, dict):
            raise BoundaryLayerLocalScheduleError(
                "local boundary-layer planner returned an invalid manifest"
            )
        manifest["started_at_utc"] = started
        manifest["ended_at_utc"] = _utc_now()
        manifest["command_inputs"] = {
            "config_path": str(config),
            "config_sha256": _sha256(config),
            "clearance_report_path": str(clearance),
            "clearance_report_sha256": str(clearance_report_sha256).casefold(),
            "output_directory": str(output),
        }
    except Exception as error:
        manifest = _command_failure_report(
            error,
            started_at_utc=started,
            config_path=config,
            clearance_report_path=clearance,
            clearance_report_sha256=clearance_report_sha256,
            output_directory=output,
        )

    report_path = output / "boundary_layer_local_schedule.json"
    manifest["report_path"] = str(report_path)
    try:
        rendered = json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as error:
        manifest = _command_failure_report(
            error,
            started_at_utc=started,
            config_path=config,
            clearance_report_path=clearance,
            clearance_report_sha256=clearance_report_sha256,
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
    "BoundaryLayerLocalScheduleError",
    "plan_local_boundary_layer_schedules",
    "run_boundary_layer_local_schedule_command",
    "solve_growth_ratio",
]
