"""Strict binding of a clearance-aware wall schedule to a coarse contract.

The planner deliberately produces evidence only; it does not authorize Gmsh.
This module is the narrow hand-off between that evidence and the calibration
strategy.  It reads one explicitly selected JSON file, verifies its SHA-256
and lineage, reconstructs every physical cumulative schedule, and returns a
self-hashed, JSON-ready binding keyed only by stable surface fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Mapping


_PLAN_SCHEMA = "cfdpipe.boundary_layer_local_schedule.v1"
_BINDING_SCHEMA = "cfdpipe.boundary_layer_local_schedule_binding.v1"
_COARSE_SCHEMA = "cfdpipe.coarse_mesh.v1"
_WALL_SURFACE_COUNT = 48
_MINIMUM_LAYER_COUNT = 15


class BoundaryLayerScheduleBindingError(RuntimeError):
    """Raised when local-stack evidence cannot safely drive subdivision."""


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
        raise BoundaryLayerScheduleBindingError(
            "local schedule binding is not canonical JSON"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.casefold())


def _matching_repository_path(value: object, expected: object) -> bool:
    raw = str(value).replace("\\", "/")
    expected_raw = str(expected).replace("\\", "/")
    for anchor in ("/config/", "/geometry/", "/runs/"):
        raw_index = raw.casefold().find(anchor)
        expected_index = expected_raw.casefold().find(anchor)
        if raw_index >= 0 and expected_index >= 0:
            return (
                raw[raw_index:].casefold()
                == expected_raw[expected_index:].casefold()
            )
    return os.path.normcase(raw) == os.path.normcase(expected_raw)


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerScheduleBindingError(f"{label} is not finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerScheduleBindingError(
            f"{label} is not finite numeric data"
        ) from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        raise BoundaryLayerScheduleBindingError(
            f"{label} must be finite" + (" and positive" if positive else "")
        )
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerScheduleBindingError(f"{label} must be a positive integer")
    return value


def _same(left: object, right: object, label: str) -> float:
    first = _finite(left, label)
    second = _finite(right, label)
    if not math.isclose(first, second, rel_tol=1.0e-11, abs_tol=1.0e-14):
        raise BoundaryLayerScheduleBindingError(f"{label} does not match its lineage")
    return first


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoundaryLayerScheduleBindingError(
                f"local schedule JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise BoundaryLayerScheduleBindingError(
        f"local schedule JSON contains non-finite constant {value}"
    )


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without resolving links or junctions."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _assert_no_link_or_junction(path: Path, label: str) -> Path:
    """Reject existing symlink/junction components without rejecting drive roots."""

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if not parts:
        raise BoundaryLayerScheduleBindingError(f"{label} path is empty")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise BoundaryLayerScheduleBindingError(
                    f"{label} path contains a symbolic link"
                )
            junction_checker = getattr(current, "is_junction", None)
            if callable(junction_checker):
                is_junction = bool(junction_checker())
            else:
                os_junction_checker = getattr(os.path, "isjunction", None)
                is_junction = bool(
                    callable(os_junction_checker) and os_junction_checker(current)
                )
            if is_junction:
                raise BoundaryLayerScheduleBindingError(
                    f"{label} path contains a junction"
                )
            if not callable(junction_checker):
                try:
                    metadata = os.lstat(current)
                except FileNotFoundError:
                    continue
                reparse_tag = getattr(metadata, "st_reparse_tag", 0)
                forbidden_tags = {
                    getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
                    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -2),
                }
                if reparse_tag in forbidden_tags:
                    raise BoundaryLayerScheduleBindingError(
                        f"{label} path contains a link or junction reparse point"
                    )
        except BoundaryLayerScheduleBindingError:
            raise
        except OSError as error:
            raise BoundaryLayerScheduleBindingError(
                f"{label} path components cannot be inspected"
            ) from error
    return absolute


def _decode_plan_payload(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoundaryLayerScheduleBindingError(
            f"local schedule JSON cannot be parsed: {error}"
        ) from error
    if not isinstance(document, dict):
        raise BoundaryLayerScheduleBindingError("local schedule JSON is not an object")
    return document


def _read_plan(
    path: str | os.PathLike[str], expected_sha256: str
) -> tuple[dict[str, Any], Path, str, int]:
    expected = str(expected_sha256).casefold()
    if not _is_sha256(expected):
        raise BoundaryLayerScheduleBindingError("local schedule SHA-256 is invalid")
    requested = Path(path).expanduser()
    _assert_no_link_or_junction(requested, "explicit local schedule")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerScheduleBindingError(
            "explicit local schedule file does not exist"
        ) from error
    if not resolved.is_file() or resolved.is_symlink():
        raise BoundaryLayerScheduleBindingError(
            "explicit local schedule path is not a regular file"
        )
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != before.st_size
    ):
        raise BoundaryLayerScheduleBindingError(
            "local schedule file changed while it was being read"
        )
    actual = _sha256_bytes(payload)
    if actual != expected:
        raise BoundaryLayerScheduleBindingError("local schedule SHA-256 is stale")
    document = _decode_plan_payload(payload)
    return document, resolved, actual, len(payload)


def _contract_inputs(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping) or contract.get("schema") != _COARSE_SCHEMA:
        raise BoundaryLayerScheduleBindingError("coarse contract schema is invalid")
    configured_hash = contract.get("normalized_config_sha256")
    unsigned = dict(contract)
    unsigned.pop("normalized_config_sha256", None)
    if not _is_sha256(configured_hash) or _canonical_sha256(unsigned) != configured_hash:
        raise BoundaryLayerScheduleBindingError("normalized coarse contract hash is stale")
    design = contract.get("boundary_layer_design")
    fingerprints = contract.get("wall_surface_fingerprints")
    if not isinstance(design, Mapping) or design.get("status") != "PASS":
        raise BoundaryLayerScheduleBindingError("coarse boundary-layer design is not PASS")
    if not isinstance(fingerprints, list):
        raise BoundaryLayerScheduleBindingError("coarse wall fingerprints are missing")
    normalized_fingerprints = [str(value).casefold() for value in fingerprints]
    if (
        len(normalized_fingerprints) != _WALL_SURFACE_COUNT
        or len(set(normalized_fingerprints)) != _WALL_SURFACE_COUNT
        or any(not _is_sha256(value) for value in normalized_fingerprints)
    ):
        raise BoundaryLayerScheduleBindingError(
            "coarse contract must contain 48 unique wall fingerprints"
        )
    first = _finite(design.get("first_layer_height_m"), "first layer height", positive=True)
    count = _positive_integer(design.get("layer_count"), "layer count")
    minimum = _positive_integer(
        design.get("project_minimum_layer_count"), "minimum layer count"
    )
    growth = _finite(design.get("growth_ratio"), "maximum growth ratio", positive=True)
    total = _finite(design.get("total_thickness_m"), "global total thickness", positive=True)
    if count < _MINIMUM_LAYER_COUNT or count < minimum or growth < 1.0:
        raise BoundaryLayerScheduleBindingError("coarse layer-count/growth policy is unsafe")
    clearance_hash = contract.get("clearance_evidence_contract_sha256")
    if not _is_sha256(clearance_hash):
        raise BoundaryLayerScheduleBindingError("clearance lineage hash is missing")
    maximum_clearance_fraction = _finite(
        contract.get("boundary_layer_maximum_clearance_fraction"),
        "maximum clearance fraction",
        positive=True,
    )
    if maximum_clearance_fraction >= 0.5:
        raise BoundaryLayerScheduleBindingError(
            "maximum clearance fraction must remain below one half"
        )
    return {
        "coarse_contract_sha256": str(configured_hash),
        "clearance_evidence_contract_sha256": str(clearance_hash),
        "fingerprints": sorted(normalized_fingerprints),
        "first_layer_height_m": first,
        "layer_count": count,
        "minimum_layer_count": minimum,
        "maximum_growth_ratio": growth,
        "global_total_thickness_m": total,
        "maximum_clearance_fraction": maximum_clearance_fraction,
        "maximum_core_to_last_layer_ratio": _finite(
            design.get("maximum_core_to_last_layer_ratio"),
            "maximum core-to-last-layer ratio",
            positive=True,
        ),
        "pipeline_brep_path": str(contract.get("pipeline_brep_path", "")),
    }


def _schedule_hash(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    configured = unsigned.pop("schedule_evidence_sha256", None)
    if not _is_sha256(configured) or _canonical_sha256(unsigned) != configured:
        raise BoundaryLayerScheduleBindingError(
            "one surface schedule evidence hash is stale"
        )
    return str(configured)


def _cumulative(first: float, count: int, growth: float) -> list[float]:
    values: list[float] = []
    running = 0.0
    for layer in range(count):
        increment = first * growth**layer
        if not math.isfinite(increment) or increment <= 0.0:
            raise BoundaryLayerScheduleBindingError(
                "surface schedule contains a non-finite layer height"
            )
        running += increment
        if not math.isfinite(running) or (values and running <= values[-1]):
            raise BoundaryLayerScheduleBindingError(
                "surface cumulative schedule is not finite and strictly increasing"
            )
        values.append(running)
    return values


def bind_boundary_layer_local_schedule(
    *,
    coarse_contract: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    source_plan_path: str | os.PathLike[str],
    source_plan_sha256: str,
    source_plan_size_bytes: int,
    expected_coarse_contract_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate one already SHA-verified plan and return a self-hashed binding."""

    expected = _contract_inputs(coarse_contract)
    if expected_coarse_contract_sha256 is not None:
        lineage_hash = str(expected_coarse_contract_sha256).casefold()
        if not _is_sha256(lineage_hash):
            raise BoundaryLayerScheduleBindingError(
                "expected coarse contract SHA-256 is invalid"
            )
        expected["coarse_contract_sha256"] = lineage_hash
    if not _is_sha256(str(source_plan_sha256).casefold()):
        raise BoundaryLayerScheduleBindingError("source plan SHA-256 is invalid")
    if not isinstance(plan_document, Mapping):
        raise BoundaryLayerScheduleBindingError("local schedule plan is not a mapping")
    if plan_document.get("schema") != _PLAN_SCHEMA or plan_document.get("status") != "PASS":
        raise BoundaryLayerScheduleBindingError("local schedule plan is not schema/PASS")
    if expected_coarse_contract_sha256 is not None:
        lineage_hash = plan_document.get("clearance_evidence_contract_sha256")
        if not _is_sha256(lineage_hash):
            raise BoundaryLayerScheduleBindingError(
                "legacy local schedule clearance lineage is invalid"
            )
        expected["clearance_evidence_contract_sha256"] = str(lineage_hash)
    safety_flags = {
        "planning_only": True,
        "extrusion_authorized": False,
        "extrusion_feasibility_claimed": False,
        "requires_builder_integration_and_mesh_quality_gate": True,
        "mesh_generated": False,
        "su2_called": False,
        "paraview_called": False,
        "production_mesh_eligible": False,
    }
    if any(plan_document.get(key) is not value for key, value in safety_flags.items()):
        raise BoundaryLayerScheduleBindingError("local schedule safety scope is invalid")
    if (
        plan_document.get("coarse_contract_sha256")
        != expected["coarse_contract_sha256"]
        or plan_document.get("clearance_evidence_contract_sha256")
        != expected["clearance_evidence_contract_sha256"]
    ):
        raise BoundaryLayerScheduleBindingError("local schedule coarse/clearance lineage is stale")
    policy = plan_document.get("policy")
    source = plan_document.get("source_geometry")
    clearance = plan_document.get("clearance_report")
    if not isinstance(policy, Mapping) or not isinstance(source, Mapping) or not isinstance(clearance, Mapping):
        raise BoundaryLayerScheduleBindingError("local schedule lineage tables are incomplete")
    if (
        policy.get("first_layer_height_reduced") is not False
        or policy.get("layer_count_reduced") is not False
        or _positive_integer(policy.get("layer_count"), "policy layer count")
        != expected["layer_count"]
        or _positive_integer(
            policy.get("minimum_layer_count"), "policy minimum layer count"
        )
        != expected["minimum_layer_count"]
    ):
        raise BoundaryLayerScheduleBindingError("local schedule reduced a fixed layer invariant")
    _same(policy.get("first_layer_height_m"), expected["first_layer_height_m"], "policy first layer")
    _same(policy.get("maximum_growth_ratio"), expected["maximum_growth_ratio"], "policy maximum growth")
    _same(policy.get("global_design_total_thickness_m"), expected["global_total_thickness_m"], "policy global total")
    _same(policy.get("maximum_clearance_fraction"), expected["maximum_clearance_fraction"], "policy clearance fraction")
    _same(
        policy.get("maximum_core_to_last_layer_ratio"),
        expected["maximum_core_to_last_layer_ratio"],
        "policy maximum core ratio",
    )
    provenance = coarse_contract.get("provenance", {})
    if (
        not isinstance(provenance, Mapping)
        or not _matching_repository_path(
            source.get("path", ""), expected["pipeline_brep_path"]
        )
        or source.get("sha256") != provenance.get("pipeline_brep_sha256")
        or source.get("unchanged_in_clearance_audit") is not True
        or source.get("gmsh_finalize_called") is not True
        or clearance.get("execution_status") != "PASS"
        or clearance.get("unchanged_during_planning") is not True
        or not _is_sha256(clearance.get("sha256"))
    ):
        raise BoundaryLayerScheduleBindingError("local schedule source/clearance evidence is stale")
    clearance_contract = clearance.get("coarse_contract_sha256")
    if clearance_contract not in {
        expected["coarse_contract_sha256"],
        expected["clearance_evidence_contract_sha256"],
    }:
        raise BoundaryLayerScheduleBindingError(
            "clearance report is not bound to the current or preserved clearance contract"
        )
    schedules = plan_document.get("schedules")
    if not isinstance(schedules, list):
        raise BoundaryLayerScheduleBindingError("surface schedules are missing")
    if (
        plan_document.get("surface_count") != _WALL_SURFACE_COUNT
        or plan_document.get("pass_surface_count") != _WALL_SURFACE_COUNT
        or plan_document.get("fail_surface_count") != 0
        or plan_document.get("sample_count") != _WALL_SURFACE_COUNT * 5
        or len(schedules) != _WALL_SURFACE_COUNT
        or plan_document.get("failure_reasons") != []
    ):
        raise BoundaryLayerScheduleBindingError("local schedule does not contain 48 PASS surfaces")

    global_cumulative = _cumulative(
        expected["first_layer_height_m"],
        expected["layer_count"],
        expected["maximum_growth_ratio"],
    )
    _same(global_cumulative[-1], expected["global_total_thickness_m"], "global cumulative total")
    bound: dict[str, dict[str, Any]] = {}
    for raw in schedules:
        if not isinstance(raw, Mapping) or raw.get("status") != "PASS":
            raise BoundaryLayerScheduleBindingError("one surface schedule is not PASS")
        if raw.get("failure_reason") is not None:
            raise BoundaryLayerScheduleBindingError(
                "one PASS surface schedule retains a failure reason"
            )
        fingerprint = str(raw.get("surface_fingerprint_id", "")).casefold()
        if not _is_sha256(fingerprint) or fingerprint in bound:
            raise BoundaryLayerScheduleBindingError("surface schedule fingerprint is invalid or duplicated")
        schedule_hash = _schedule_hash(raw)
        if raw.get("coarse_contract_sha256") != expected["coarse_contract_sha256"]:
            raise BoundaryLayerScheduleBindingError("surface schedule coarse lineage is stale")
        if raw.get("clearance_report_sha256") != clearance.get("sha256"):
            raise BoundaryLayerScheduleBindingError("surface schedule clearance lineage is stale")
        count = _positive_integer(raw.get("layer_count"), "surface layer count")
        if count != expected["layer_count"] or count < _MINIMUM_LAYER_COUNT:
            raise BoundaryLayerScheduleBindingError("surface layer count violates the fixed contract")
        first = _same(raw.get("first_layer_height_m"), expected["first_layer_height_m"], "surface first layer")
        maximum_growth = _same(raw.get("maximum_growth_ratio"), expected["maximum_growth_ratio"], "surface maximum growth")
        growth = _finite(raw.get("local_growth_ratio"), "local growth ratio", positive=True)
        if growth < 1.0 or growth > maximum_growth:
            raise BoundaryLayerScheduleBindingError("surface local growth amplifies the project schedule")
        cumulative = _cumulative(first, count, growth)
        total = _same(raw.get("total_thickness_m"), cumulative[-1], "surface total thickness")
        _same(raw.get("global_design_total_thickness_m"), expected["global_total_thickness_m"], "surface global total")
        _same(
            raw.get("minimum_fixed_stack_total_thickness_m"),
            first * count,
            "surface fixed-stack minimum",
        )
        clearance_value = _finite(raw.get("minimum_clearance_lower_bound_m"), "surface clearance", positive=True)
        fraction = _same(raw.get("maximum_clearance_fraction"), expected["maximum_clearance_fraction"], "surface clearance fraction")
        target = min(expected["global_total_thickness_m"], clearance_value * fraction)
        _same(raw.get("clearance_limited_target_total_thickness_m"), target, "surface clearance target")
        _same(total, target, "surface target total")
        last = _same(raw.get("last_layer_height_m"), cumulative[-1] - cumulative[-2], "surface last layer")
        core_ratio = _same(
            raw.get("maximum_core_to_last_layer_ratio"),
            expected["maximum_core_to_last_layer_ratio"],
            "surface maximum core ratio",
        )
        _same(
            raw.get("recommended_local_core_max_m"),
            last * core_ratio,
            "surface recommended local core maximum",
        )
        if not _is_sha256(raw.get("surface_clearance_evidence_sha256")):
            raise BoundaryLayerScheduleBindingError(
                "surface clearance evidence SHA-256 is invalid"
            )
        _same(raw.get("clearance_margin_m"), clearance_value - total, "surface clearance margin")
        two_sided = _same(raw.get("two_sided_core_clearance_margin_m"), clearance_value - 2.0 * total, "surface two-sided margin")
        if two_sided <= 0.0:
            raise BoundaryLayerScheduleBindingError("surface schedule leaves no conservative two-sided clearance")
        if any(value > global_cumulative[index] and not math.isclose(value, global_cumulative[index], rel_tol=1.0e-12, abs_tol=1.0e-15) for index, value in enumerate(cumulative)):
            raise BoundaryLayerScheduleBindingError("surface cumulative schedule amplifies the global plan")
        bound[fingerprint] = {
            "surface_fingerprint_id": fingerprint,
            "schedule_evidence_sha256": schedule_hash,
            "layer_count": count,
            "first_layer_height_m": first,
            "local_growth_ratio": growth,
            "total_thickness_m": total,
            "last_layer_height_m": last,
            "cumulative_heights_m": cumulative,
            "clearance_limited": total < expected["global_total_thickness_m"] and not math.isclose(total, expected["global_total_thickness_m"], rel_tol=1.0e-12, abs_tol=1.0e-15),
        }
    if sorted(bound) != expected["fingerprints"]:
        raise BoundaryLayerScheduleBindingError("surface schedule coverage differs from all 48 walls")

    binding: dict[str, Any] = {
        "schema": _BINDING_SCHEMA,
        "status": "PASS",
        "source_plan_schema": _PLAN_SCHEMA,
        "source_plan_path": str(Path(source_plan_path).resolve()),
        "source_plan_sha256": str(source_plan_sha256).casefold(),
        "source_plan_size_bytes": _positive_integer(source_plan_size_bytes, "source plan size"),
        "coarse_contract_sha256": expected["coarse_contract_sha256"],
        "clearance_evidence_contract_sha256": expected["clearance_evidence_contract_sha256"],
        "clearance_report_sha256": str(clearance["sha256"]),
        "surface_count": len(bound),
        "layer_count": expected["layer_count"],
        "minimum_layer_count": expected["minimum_layer_count"],
        "first_layer_height_m": expected["first_layer_height_m"],
        "surface_schedules": {key: bound[key] for key in sorted(bound)},
        "limited_surface_count": sum(record["clearance_limited"] for record in bound.values()),
        "minimum_total_thickness_m": min(record["total_thickness_m"] for record in bound.values()),
        "maximum_total_thickness_m": max(record["total_thickness_m"] for record in bound.values()),
        "runtime_entity_tags_present": False,
        "su2_called": False,
        "paraview_called": False,
    }
    binding["binding_sha256"] = _canonical_sha256(binding)
    return binding


def validate_boundary_layer_schedule_binding(
    coarse_contract: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    expected_coarse_contract_sha256: str | None = None,
) -> dict[str, Any]:
    """Revalidate a binding at its calibration-strategy consumption point."""

    expected = _contract_inputs(coarse_contract)
    if not isinstance(binding, Mapping) or binding.get("schema") != _BINDING_SCHEMA or binding.get("status") != "PASS":
        raise BoundaryLayerScheduleBindingError("local schedule binding is missing or not PASS")
    if expected_coarse_contract_sha256 is not None:
        lineage_hash = str(expected_coarse_contract_sha256).casefold()
        clearance_hash = binding.get("clearance_evidence_contract_sha256")
        if not _is_sha256(lineage_hash) or not _is_sha256(clearance_hash):
            raise BoundaryLayerScheduleBindingError(
                "legacy local schedule binding lineage is invalid"
            )
        expected["coarse_contract_sha256"] = lineage_hash
        expected["clearance_evidence_contract_sha256"] = str(clearance_hash)
    unsigned = dict(binding)
    configured = unsigned.pop("binding_sha256", None)
    if not _is_sha256(configured) or _canonical_sha256(unsigned) != configured:
        raise BoundaryLayerScheduleBindingError("local schedule binding hash is stale")
    if (
        binding.get("coarse_contract_sha256") != expected["coarse_contract_sha256"]
        or binding.get("clearance_evidence_contract_sha256") != expected["clearance_evidence_contract_sha256"]
        or binding.get("surface_count") != _WALL_SURFACE_COUNT
        or binding.get("layer_count") != expected["layer_count"]
        or binding.get("minimum_layer_count") != expected["minimum_layer_count"]
        or binding.get("runtime_entity_tags_present") is not False
    ):
        raise BoundaryLayerScheduleBindingError("local schedule binding lineage is stale")
    _same(binding.get("first_layer_height_m"), expected["first_layer_height_m"], "binding first layer")
    if (
        binding.get("source_plan_schema") != _PLAN_SCHEMA
        or not _is_sha256(binding.get("source_plan_sha256"))
        or binding.get("su2_called") is not False
        or binding.get("paraview_called") is not False
    ):
        raise BoundaryLayerScheduleBindingError("bound source-plan scope is invalid")
    source_path = Path(str(binding.get("source_plan_path", ""))).expanduser()
    _assert_no_link_or_junction(source_path, "bound source-plan")
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerScheduleBindingError(
            "bound source-plan file no longer exists"
        ) from error
    if not source_path.is_file() or source_path.is_symlink():
        raise BoundaryLayerScheduleBindingError("bound source-plan path is not a file")
    before = source_path.stat()
    payload = source_path.read_bytes()
    after = source_path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != before.st_size
        or len(payload) != binding.get("source_plan_size_bytes")
        or _sha256_bytes(payload) != binding.get("source_plan_sha256")
    ):
        raise BoundaryLayerScheduleBindingError("bound source-plan file SHA-256 is stale")
    plan_document = _decode_plan_payload(payload)
    schedules = binding.get("surface_schedules")
    if not isinstance(schedules, Mapping) or sorted(str(key) for key in schedules) != expected["fingerprints"]:
        raise BoundaryLayerScheduleBindingError("local schedule binding surface coverage is incomplete")
    global_cumulative = _cumulative(
        expected["first_layer_height_m"],
        expected["layer_count"],
        expected["maximum_growth_ratio"],
    )
    totals: list[float] = []
    limited_count = 0
    for fingerprint, record in schedules.items():
        if not isinstance(record, Mapping) or record.get("surface_fingerprint_id") != fingerprint:
            raise BoundaryLayerScheduleBindingError("bound surface schedule identity is inconsistent")
        cumulative = record.get("cumulative_heights_m")
        if not isinstance(cumulative, list) or len(cumulative) != expected["layer_count"]:
            raise BoundaryLayerScheduleBindingError("bound cumulative schedule is incomplete")
        values = [_finite(value, "bound cumulative height", positive=True) for value in cumulative]
        if any(right <= left for left, right in zip(values, values[1:])):
            raise BoundaryLayerScheduleBindingError("bound cumulative schedule is not strictly increasing")
        _same(values[0], expected["first_layer_height_m"], "bound first layer")
        _same(values[-1], record.get("total_thickness_m"), "bound total thickness")
        growth = _finite(record.get("local_growth_ratio"), "bound local growth", positive=True)
        if growth < 1.0 or growth > expected["maximum_growth_ratio"]:
            raise BoundaryLayerScheduleBindingError("bound local growth amplifies the global plan")
        reconstructed = _cumulative(
            expected["first_layer_height_m"], expected["layer_count"], growth
        )
        if any(
            not math.isclose(left, right, rel_tol=1.0e-11, abs_tol=1.0e-14)
            for left, right in zip(values, reconstructed)
        ):
            raise BoundaryLayerScheduleBindingError(
                "bound cumulative heights do not match local growth"
            )
        if any(
            value > global_cumulative[index]
            and not math.isclose(
                value,
                global_cumulative[index],
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            )
            for index, value in enumerate(values)
        ):
            raise BoundaryLayerScheduleBindingError(
                "bound cumulative schedule amplifies a surface plan"
            )
        if not _is_sha256(record.get("schedule_evidence_sha256")):
            raise BoundaryLayerScheduleBindingError(
                "bound surface schedule evidence SHA-256 is invalid"
            )
        totals.append(values[-1])
        limited_count += int(record.get("clearance_limited") is True)
    if (
        binding.get("limited_surface_count") != limited_count
        or not math.isclose(
            _finite(binding.get("minimum_total_thickness_m"), "binding minimum total"),
            min(totals),
            rel_tol=1.0e-11,
            abs_tol=1.0e-14,
        )
        or not math.isclose(
            _finite(binding.get("maximum_total_thickness_m"), "binding maximum total"),
            max(totals),
            rel_tol=1.0e-11,
            abs_tol=1.0e-14,
        )
    ):
        raise BoundaryLayerScheduleBindingError("local schedule binding summary is stale")
    rebuilt = bind_boundary_layer_local_schedule(
        coarse_contract=coarse_contract,
        plan_document=plan_document,
        source_plan_path=source_path,
        source_plan_sha256=str(binding["source_plan_sha256"]),
        source_plan_size_bytes=len(payload),
        expected_coarse_contract_sha256=expected_coarse_contract_sha256,
    )
    if dict(binding) != rebuilt:
        raise BoundaryLayerScheduleBindingError(
            "local schedule binding differs from the authoritative source plan"
        )
    return rebuilt


def validate_boundary_layer_schedule_binding_for_strategy(
    strategy_config: Mapping[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    """Revalidate a bound plan at the final strategy consumption point.

    The full coarse contract is revalidated by the orchestration layer.  This
    second gate deliberately uses only data carried into the strategy: its
    canonical config, the immutable source-plan bytes and the embedded binding.
    """

    if (
        not isinstance(strategy_config, Mapping)
        or strategy_config.get("schema") != _COARSE_SCHEMA
    ):
        raise BoundaryLayerScheduleBindingError("strategy config schema is invalid")
    configured_config_hash = strategy_config.get("normalized_config_sha256")
    unsigned_config = dict(strategy_config)
    unsigned_config.pop("normalized_config_sha256", None)
    if (
        not _is_sha256(configured_config_hash)
        or _canonical_sha256(unsigned_config) != configured_config_hash
    ):
        raise BoundaryLayerScheduleBindingError("strategy config hash is stale")

    configured_contract_hash = strategy_config.get("coarse_contract_sha256")
    raw_fingerprints = strategy_config.get("wall_surface_fingerprints")
    if not _is_sha256(configured_contract_hash) or not isinstance(
        raw_fingerprints, list
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy config lacks authoritative schedule lineage"
        )
    fingerprints = [str(value) for value in raw_fingerprints]
    if (
        len(fingerprints) != _WALL_SURFACE_COUNT
        or len(set(fingerprints)) != _WALL_SURFACE_COUNT
        or any(
            value != value.casefold() or not _is_sha256(value)
            for value in fingerprints
        )
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy config must contain 48 unique lowercase wall fingerprints"
        )
    expected_fingerprints = sorted(fingerprints)
    layer_count = _positive_integer(
        strategy_config.get("layer_count"), "strategy layer count"
    )
    first_height = _finite(
        strategy_config.get("first_layer_height_m"),
        "strategy first-layer height",
        positive=True,
    )
    maximum_growth = _finite(
        strategy_config.get("growth_ratio"),
        "strategy maximum growth ratio",
        positive=True,
    )
    if layer_count < _MINIMUM_LAYER_COUNT or maximum_growth < 1.0:
        raise BoundaryLayerScheduleBindingError(
            "strategy layer-count/growth policy is unsafe"
        )

    if (
        not isinstance(binding, Mapping)
        or binding.get("schema") != _BINDING_SCHEMA
        or binding.get("status") != "PASS"
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy local schedule binding is missing or not PASS"
        )
    unsigned_binding = dict(binding)
    configured_binding_hash = unsigned_binding.pop("binding_sha256", None)
    if (
        not _is_sha256(configured_binding_hash)
        or _canonical_sha256(unsigned_binding) != configured_binding_hash
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy local schedule binding hash is stale"
        )
    if (
        binding.get("coarse_contract_sha256") != configured_contract_hash
        or binding.get("source_plan_schema") != _PLAN_SCHEMA
        or binding.get("surface_count") != _WALL_SURFACE_COUNT
        or binding.get("layer_count") != layer_count
        or _positive_integer(
            binding.get("minimum_layer_count"), "bound minimum layer count"
        )
        < _MINIMUM_LAYER_COUNT
        or binding.get("runtime_entity_tags_present") is not False
        or binding.get("su2_called") is not False
        or binding.get("paraview_called") is not False
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy local schedule binding lineage or scope is stale"
        )
    _same(
        binding.get("first_layer_height_m"),
        first_height,
        "strategy-bound first layer",
    )

    document, resolved, actual_sha256, actual_size = _read_plan(
        str(binding.get("source_plan_path", "")),
        str(binding.get("source_plan_sha256", "")),
    )
    if (
        str(resolved) != str(binding.get("source_plan_path", ""))
        or actual_sha256 != binding.get("source_plan_sha256")
        or actual_size != binding.get("source_plan_size_bytes")
        or document.get("schema") != _PLAN_SCHEMA
        or document.get("status") != "PASS"
        or document.get("planning_only") is not True
        or document.get("extrusion_authorized") is not False
        or document.get("mesh_generated") is not False
        or document.get("su2_called") is not False
        or document.get("paraview_called") is not False
        or document.get("production_mesh_eligible") is not False
        or document.get("coarse_contract_sha256") != configured_contract_hash
        or document.get("surface_count") != _WALL_SURFACE_COUNT
        or document.get("pass_surface_count") != _WALL_SURFACE_COUNT
        or document.get("fail_surface_count") != 0
        or document.get("failure_reasons") != []
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy source plan scope or lineage is stale"
        )

    raw_schedules = binding.get("surface_schedules")
    raw_keys = list(raw_schedules) if isinstance(raw_schedules, Mapping) else []
    if (
        not isinstance(raw_schedules, Mapping)
        or len(raw_keys) != _WALL_SURFACE_COUNT
        or len(set(raw_keys)) != _WALL_SURFACE_COUNT
        or any(
            not isinstance(value, str)
            or value != value.casefold()
            or not _is_sha256(value)
            for value in raw_keys
        )
        or sorted(raw_keys) != expected_fingerprints
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy local schedule keys are incomplete, noncanonical, or duplicated"
        )
    source_schedules = document.get("schedules")
    if (
        not isinstance(source_schedules, list)
        or len(source_schedules) != _WALL_SURFACE_COUNT
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy source plan has incomplete surface schedules"
        )
    source_by_fingerprint: dict[str, Mapping[str, Any]] = {}
    for raw_record in source_schedules:
        if not isinstance(raw_record, Mapping):
            raise BoundaryLayerScheduleBindingError(
                "strategy source plan contains a non-object schedule"
            )
        fingerprint = raw_record.get("surface_fingerprint_id")
        if (
            not isinstance(fingerprint, str)
            or fingerprint != fingerprint.casefold()
            or not _is_sha256(fingerprint)
            or fingerprint in source_by_fingerprint
            or raw_record.get("status") != "PASS"
            or raw_record.get("failure_reason") is not None
            or raw_record.get("coarse_contract_sha256")
            != configured_contract_hash
        ):
            raise BoundaryLayerScheduleBindingError(
                "strategy source plan surface identity or lineage is invalid"
            )
        _schedule_hash(raw_record)
        source_by_fingerprint[fingerprint] = raw_record
    if sorted(source_by_fingerprint) != expected_fingerprints:
        raise BoundaryLayerScheduleBindingError(
            "strategy source plan does not cover the authoritative wall inventory"
        )

    totals: list[float] = []
    limited_count = 0
    global_cumulative = _cumulative(first_height, layer_count, maximum_growth)
    for fingerprint in expected_fingerprints:
        bound_record = raw_schedules[fingerprint]
        source_record = source_by_fingerprint[fingerprint]
        if (
            not isinstance(bound_record, Mapping)
            or bound_record.get("surface_fingerprint_id") != fingerprint
            or bound_record.get("schedule_evidence_sha256")
            != source_record.get("schedule_evidence_sha256")
            or bound_record.get("layer_count") != layer_count
        ):
            raise BoundaryLayerScheduleBindingError(
                "strategy bound surface identity differs from its source plan"
            )
        _same(
            bound_record.get("first_layer_height_m"),
            first_height,
            "strategy bound surface first layer",
        )
        source_growth = _finite(
            source_record.get("local_growth_ratio"),
            "source local growth ratio",
            positive=True,
        )
        bound_growth = _same(
            bound_record.get("local_growth_ratio"),
            source_growth,
            "strategy bound local growth ratio",
        )
        if bound_growth < 1.0 or bound_growth > maximum_growth:
            raise BoundaryLayerScheduleBindingError(
                "strategy bound local growth amplifies the project schedule"
            )
        expected_cumulative = _cumulative(first_height, layer_count, bound_growth)
        raw_cumulative = bound_record.get("cumulative_heights_m")
        if not isinstance(raw_cumulative, list) or len(raw_cumulative) != layer_count:
            raise BoundaryLayerScheduleBindingError(
                "strategy bound cumulative schedule is incomplete"
            )
        cumulative = [
            _finite(value, "strategy bound cumulative height", positive=True)
            for value in raw_cumulative
        ]
        if any(
            not math.isclose(left, right, rel_tol=1.0e-11, abs_tol=1.0e-14)
            for left, right in zip(cumulative, expected_cumulative)
        ) or any(
            value > global_cumulative[index]
            and not math.isclose(
                value,
                global_cumulative[index],
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            )
            for index, value in enumerate(cumulative)
        ):
            raise BoundaryLayerScheduleBindingError(
                "strategy bound cumulative schedule differs from its physical plan"
            )
        total = _same(
            bound_record.get("total_thickness_m"),
            source_record.get("total_thickness_m"),
            "strategy bound total thickness",
        )
        _same(total, cumulative[-1], "strategy cumulative total thickness")
        global_total = _finite(
            source_record.get("global_design_total_thickness_m"),
            "source global design total thickness",
            positive=True,
        )
        expected_limited = total < global_total and not math.isclose(
            total,
            global_total,
            rel_tol=1.0e-12,
            abs_tol=1.0e-15,
        )
        if bound_record.get("clearance_limited") is not expected_limited:
            raise BoundaryLayerScheduleBindingError(
                "strategy bound clearance-limited flag is stale"
            )
        totals.append(total)
        limited_count += int(expected_limited)
    if (
        binding.get("limited_surface_count") != limited_count
        or not math.isclose(
            _finite(
                binding.get("minimum_total_thickness_m"),
                "strategy binding minimum total",
                positive=True,
            ),
            min(totals),
            rel_tol=1.0e-11,
            abs_tol=1.0e-14,
        )
        or not math.isclose(
            _finite(
                binding.get("maximum_total_thickness_m"),
                "strategy binding maximum total",
                positive=True,
            ),
            max(totals),
            rel_tol=1.0e-11,
            abs_tol=1.0e-14,
        )
    ):
        raise BoundaryLayerScheduleBindingError(
            "strategy local schedule binding summary is stale"
        )
    return dict(binding)


def load_and_bind_boundary_layer_local_schedule(
    *,
    coarse_contract: Mapping[str, Any],
    plan_path: str | os.PathLike[str],
    plan_sha256: str,
    expected_coarse_contract_sha256: str | None = None,
) -> dict[str, Any]:
    """Read one explicit plan file and bind it to ``coarse_contract``."""

    document, resolved, actual, size = _read_plan(plan_path, plan_sha256)
    return bind_boundary_layer_local_schedule(
        coarse_contract=coarse_contract,
        plan_document=document,
        source_plan_path=resolved,
        source_plan_sha256=actual,
        source_plan_size_bytes=size,
        expected_coarse_contract_sha256=expected_coarse_contract_sha256,
    )


__all__ = [
    "BoundaryLayerScheduleBindingError",
    "bind_boundary_layer_local_schedule",
    "load_and_bind_boundary_layer_local_schedule",
    "validate_boundary_layer_schedule_binding",
    "validate_boundary_layer_schedule_binding_for_strategy",
]
