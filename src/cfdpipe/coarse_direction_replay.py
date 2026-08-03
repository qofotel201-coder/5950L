"""Fail-closed bridge from an audit-only direction result to mesh replay.

The audit manifest remains immutable and audit-only.  This module derives a
separate, self-hashed replay approval containing only stable geometry
identities and exact binary64 direction encodings.  Runtime Gmsh tags are
deliberately excluded.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .coarse_component_direction import (
    CoarseComponentDirectionError,
    validate_component_direction_discovery,
)
from .coarse_direction_feasibility import (
    CoarseDirectionFeasibilityError,
    validate_owner_free_direction_endpoint,
)
from .coarse_schedule_feasibility import (
    CoarseScheduleFeasibilityError,
    validate_minimum_growth_endpoint,
)


APPROVAL_SCHEMA = "cfdpipe.coarse_direction_replay_approval.v1"
PHYSICAL_DISCOVERY_SCHEMA = (
    "cfdpipe.coarse_physical_schedule_fixed_direction_discovery.v1"
)
HOMOTOPY_ENDPOINT_SCHEMA = "cfdpipe.coarse_physical_schedule_homotopy_endpoint.v1"
HOMOTOPY_DISCOVERY_SCHEMA = (
    "cfdpipe.coarse_physical_schedule_fixed_direction_homotopy_discovery.v1"
)
# Compatibility spelling for callers which qualify the discovery by its
# physical-schedule scope.  The schema value itself has one source of truth.
PHYSICAL_HOMOTOPY_DISCOVERY_SCHEMA = HOMOTOPY_DISCOVERY_SCHEMA
HOMOTOPY_APPLICATION_SCHEMA = (
    "cfdpipe.coarse_physical_schedule_homotopy_application.v1"
)
HOMOTOPY_MODE = "minimum_growth_to_authoritative_physical_linear_cumulative_v1"
HOMOTOPY_AUDIT_VARIANT = "physical_local_schedule_fixed_direction_homotopy"
HOMOTOPY_INCOMPLETE_REASON = "PHYSICAL_SCHEDULE_HOMOTOPY_QUALITY_FAILED"
DEFAULT_PHYSICAL_SCHEDULE_HOMOTOPY_FRACTIONS = (
    0.0,
    2.0**-12,
    2.0**-11,
    2.0**-10,
    2.0**-9,
    2.0**-8,
    2.0**-7,
    2.0**-6,
    2.0**-5,
    2.0**-4,
    2.0**-3,
    2.0**-2,
    2.0**-1,
    1.0,
)
AUDIT_MANIFEST_SCHEMA = "cfdpipe.coarse_repair_audit_manifest.v1"
PATTERN_REQUEST = "minimum-growth-owner-free-component-pattern-audit"
PATTERN_PURPOSE = "minimum_growth_owner_free_component_pattern_audit"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE = math.ulp(1.0)


class CoarseDirectionReplayError(RuntimeError):
    """The direction audit cannot safely authorize a deterministic replay."""


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CoarseDirectionReplayError(
            "direction replay evidence is not canonically serializable"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_PATH_DERIVED_HASH_KEYS = {
    "baseline_binding_sha256",
    "binding_sha256",
    "endpoint_sha256",
    "evidence_sha256",
    "normalized_config_sha256",
    "schedule_endpoint_sha256",
    "strategy_config_sha256",
}


def _portable_attestation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _portable_attestation(item)
            for key, item in value.items()
            if key not in _PATH_DERIVED_HASH_KEYS
        }
    if isinstance(value, list):
        return [_portable_attestation(item) for item in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        for anchor in ("/config/", "/geometry/", "/runs/"):
            index = normalized.casefold().find(anchor)
            if index >= 0:
                return normalized[index:].casefold()
        return value
    return value


def _same_portable_attestation(left: Any, right: Any) -> bool:
    return _portable_attestation(left) == _portable_attestation(right)


def _is_absolute_attested_path(value: object) -> bool:
    raw = str(value)
    return Path(raw).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", raw) is not None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoarseDirectionReplayError(
                f"direction audit JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CoarseDirectionReplayError(
        f"direction audit JSON contains non-finite constant {value}"
    )


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CoarseDirectionReplayError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseDirectionReplayError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise CoarseDirectionReplayError(f"{label} must be finite")
    return result


def _float_hex_vector(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) != 3:
        raise CoarseDirectionReplayError(f"{label} must contain three values")
    vector = [_finite(raw, label) for raw in value]
    norm = math.sqrt(math.fsum(component * component for component in vector))
    if not math.isclose(norm, 1.0, rel_tol=1.0e-12, abs_tol=1.0e-12):
        raise CoarseDirectionReplayError(f"{label} is not a unit vector")
    return [component.hex() for component in vector]


def _decode_canonical_unit_hex(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise CoarseDirectionReplayError(f"{label} must contain three values")
    try:
        decoded = [float.fromhex(str(component)) for component in value]
    except ValueError as error:
        raise CoarseDirectionReplayError(
            f"{label} is not canonical binary64 hex"
        ) from error
    if _float_hex_vector(decoded, label) != value:
        raise CoarseDirectionReplayError(
            f"{label} is not canonical binary64 hex"
        )
    return decoded


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _reject_link_components(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and _is_link_like(candidate):
            raise CoarseDirectionReplayError(
                "direction audit path contains a symbolic link or junction"
            )


_QUALITY_FIELDS = {
    "status",
    "minimum_prism_scaled_jacobian_for_pass",
    "minimum_core_tetra_gamma_for_pass",
    "all_values_finite",
    "nonfinite_count",
    "nonpositive_element_count",
    "prism_below_threshold_element_count",
    "core_tetra_below_gamma_count",
    "minimum_prism_scaled_jacobian",
    "minimum_core_tetra_gamma",
    "prism_element_count",
    "core_element_count",
    "core_tetra_count",
    "maximum_prism_scaled_jacobian_deficit",
    "prism_scaled_jacobian_deficit_l1",
    "prism_scaled_jacobian_deficit_l2",
    "maximum_core_tetra_gamma_deficit",
    "core_tetra_gamma_deficit_l2",
    "quality_sha256",
}


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoarseDirectionReplayError(f"{label} must be a non-negative integer")
    return value


def _validate_quality_snapshot(
    value: Any,
    *,
    label: str,
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
    expected_prism_element_count: int | None = None,
    expected_core_element_count: int | None = None,
    require_pass: bool = False,
) -> dict[str, Any]:
    """Validate and independently hash one complete owner-free quality object."""

    if not isinstance(value, Mapping) or set(value) != _QUALITY_FIELDS:
        raise CoarseDirectionReplayError(f"{label} has an incomplete field set")
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("quality_sha256", "")).casefold()
    if (
        _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
    ):
        raise CoarseDirectionReplayError(f"{label} quality hash is stale")

    expected_prism_threshold = _finite(
        expected_minimum_prism_scaled_jacobian,
        f"{label} expected prism threshold",
    )
    expected_core_threshold = _finite(
        expected_minimum_core_tetra_gamma,
        f"{label} expected core threshold",
    )
    prism_threshold = _finite(
        value.get("minimum_prism_scaled_jacobian_for_pass"),
        f"{label} prism threshold",
    )
    core_threshold = _finite(
        value.get("minimum_core_tetra_gamma_for_pass"),
        f"{label} core threshold",
    )
    if (
        prism_threshold.hex() != expected_prism_threshold.hex()
        or core_threshold.hex() != expected_core_threshold.hex()
        or prism_threshold <= 0.0
        or core_threshold <= 0.0
    ):
        raise CoarseDirectionReplayError(f"{label} quality thresholds differ")

    count_fields = (
        "nonfinite_count",
        "nonpositive_element_count",
        "prism_below_threshold_element_count",
        "core_tetra_below_gamma_count",
        "prism_element_count",
        "core_element_count",
        "core_tetra_count",
    )
    counts = {
        field: _nonnegative_int(value.get(field), f"{label} {field}")
        for field in count_fields
    }
    prism_count = counts["prism_element_count"]
    core_count = counts["core_element_count"]
    tetra_count = counts["core_tetra_count"]
    total_count = prism_count + core_count
    if (
        prism_count <= 0
        or core_count <= 0
        or tetra_count != core_count
        or (
            expected_prism_element_count is not None
            and prism_count != expected_prism_element_count
        )
        or (
            expected_core_element_count is not None
            and core_count != expected_core_element_count
        )
        or counts["nonfinite_count"] > total_count
        or counts["nonpositive_element_count"] > total_count
        or counts["prism_below_threshold_element_count"] > prism_count
        or counts["core_tetra_below_gamma_count"] > tetra_count
    ):
        raise CoarseDirectionReplayError(f"{label} quality counts are inconsistent")

    if not isinstance(value.get("all_values_finite"), bool):
        raise CoarseDirectionReplayError(f"{label} finite flag is not boolean")
    all_finite = value["all_values_finite"]
    if all_finite is not (counts["nonfinite_count"] == 0):
        raise CoarseDirectionReplayError(f"{label} finite flag/count disagree")

    minimum_prism = _finite(
        value.get("minimum_prism_scaled_jacobian"), f"{label} prism minimum"
    )
    minimum_core = _finite(
        value.get("minimum_core_tetra_gamma"), f"{label} core minimum"
    )
    deficit_fields = (
        "maximum_prism_scaled_jacobian_deficit",
        "prism_scaled_jacobian_deficit_l1",
        "prism_scaled_jacobian_deficit_l2",
        "maximum_core_tetra_gamma_deficit",
        "core_tetra_gamma_deficit_l2",
    )
    deficits = {
        field: _finite(value.get(field), f"{label} {field}")
        for field in deficit_fields
    }
    if any(number < 0.0 for number in deficits.values()):
        raise CoarseDirectionReplayError(f"{label} quality deficit is negative")

    prism_below = counts["prism_below_threshold_element_count"]
    core_below = counts["core_tetra_below_gamma_count"]
    if (
        (prism_below == 0 and minimum_prism < prism_threshold)
        or (core_below == 0 and minimum_core < core_threshold)
        or (all_finite and prism_below > 0 and minimum_prism >= prism_threshold)
        or (all_finite and core_below > 0 and minimum_core >= core_threshold)
    ):
        raise CoarseDirectionReplayError(f"{label} minima and below-counts disagree")
    if all_finite:
        expected_prism_deficit = max(0.0, prism_threshold - minimum_prism)
        expected_core_deficit = max(0.0, core_threshold - minimum_core)
        if (
            not math.isclose(
                deficits["maximum_prism_scaled_jacobian_deficit"],
                expected_prism_deficit,
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            )
            or not math.isclose(
                deficits["maximum_core_tetra_gamma_deficit"],
                expected_core_deficit,
                rel_tol=1.0e-12,
                abs_tol=1.0e-15,
            )
            or deficits["prism_scaled_jacobian_deficit_l1"]
            + 1.0e-15
            < deficits["maximum_prism_scaled_jacobian_deficit"]
            or deficits["prism_scaled_jacobian_deficit_l2"]
            + 1.0e-15
            < deficits["maximum_prism_scaled_jacobian_deficit"] ** 2
            or deficits["core_tetra_gamma_deficit_l2"]
            + 1.0e-15
            < deficits["maximum_core_tetra_gamma_deficit"] ** 2
        ):
            raise CoarseDirectionReplayError(
                f"{label} quality deficit summaries are inconsistent"
            )

    computed_status = (
        "PASS"
        if all_finite
        and counts["nonpositive_element_count"] == 0
        and prism_below == 0
        and core_below == 0
        else "FAIL"
    )
    if value.get("status") != computed_status or (
        require_pass and computed_status != "PASS"
    ):
        raise CoarseDirectionReplayError(f"{label} quality status is inconsistent")
    return copy.deepcopy(dict(value))


def _normalize_expected_surface_schedules(
    value: Any,
    *,
    expected_surface_count: int,
    expected_layer_count: int,
) -> dict[str, list[float]]:
    if not isinstance(value, Mapping):
        raise CoarseDirectionReplayError("expected surface schedules are missing")
    normalized: dict[str, list[float]] = {}
    for raw_fingerprint, raw_schedule in value.items():
        fingerprint = str(raw_fingerprint).casefold()
        if (
            _SHA256.fullmatch(fingerprint) is None
            or fingerprint in normalized
            or not isinstance(raw_schedule, (list, tuple))
            or len(raw_schedule) != expected_layer_count
        ):
            raise CoarseDirectionReplayError(
                "one expected surface schedule has an invalid identity or length"
            )
        schedule = [
            _finite(item, "expected surface cumulative height")
            for item in raw_schedule
        ]
        if any(item <= 0.0 for item in schedule) or any(
            right <= left for left, right in zip(schedule, schedule[1:])
        ):
            raise CoarseDirectionReplayError(
                "one expected surface schedule is not strictly increasing"
            )
        normalized[fingerprint] = schedule
    if len(normalized) != expected_surface_count:
        raise CoarseDirectionReplayError(
            "expected surface schedule coverage is incomplete"
        )
    return dict(sorted(normalized.items()))


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseDirectionReplayError(f"{label} must be a positive integer")
    return value


def make_physical_schedule_homotopy_endpoint(
    *,
    baseline_binding_sha256: str,
    first_layer_height_m: float,
    layer_count: int,
) -> dict[str, Any]:
    """Create the fixed audit-only bridge from growth=1 to the bound schedule.

    The endpoint freezes a small, deterministic set of dyadic fractions.  It
    authorizes neither a direction search nor an inference that mesh quality
    is monotone between samples.
    """

    binding = str(baseline_binding_sha256).casefold()
    first = _finite(first_layer_height_m, "homotopy first layer height")
    layers = _positive_int(layer_count, "homotopy layer count")
    if _SHA256.fullmatch(binding) is None:
        raise CoarseDirectionReplayError(
            "homotopy baseline binding SHA-256 is invalid"
        )
    if first <= 0.0:
        raise CoarseDirectionReplayError(
            "homotopy first layer height must be positive"
        )
    endpoint: dict[str, Any] = {
        "schema": HOMOTOPY_ENDPOINT_SCHEMA,
        "status": "AUDIT_ONLY",
        "mode": HOMOTOPY_MODE,
        "baseline_binding_sha256": binding,
        "first_layer_height_m": first,
        "layer_count": layers,
        "fractions": list(DEFAULT_PHYSICAL_SCHEDULE_HOMOTOPY_FRACTIONS),
        "preserve_first_layer_height": True,
        "preserve_layer_count": True,
        "absolute_schedule_rebuild_required": True,
        "ascending_descending_replay_required": True,
        "quality_monotonicity_assumed": False,
        "direction_search_performed": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def validate_physical_schedule_homotopy_endpoint(
    value: Any,
    *,
    expected_binding_sha256: str,
    expected_first_layer_height_m: float,
    expected_layer_count: int,
) -> dict[str, Any]:
    """Validate the exact physical-schedule homotopy endpoint."""

    if not isinstance(value, Mapping):
        raise CoarseDirectionReplayError(
            "physical schedule homotopy endpoint must be an object"
        )
    expected = make_physical_schedule_homotopy_endpoint(
        baseline_binding_sha256=expected_binding_sha256,
        first_layer_height_m=expected_first_layer_height_m,
        layer_count=expected_layer_count,
    )
    if set(value) != set(expected) or dict(value) != expected:
        raise CoarseDirectionReplayError(
            "physical schedule homotopy endpoint is stale or unsafe"
        )
    return copy.deepcopy(expected)


def interpolate_physical_schedule_homotopy(
    baseline_surface_schedules: Mapping[str, Sequence[float]],
    endpoint: Mapping[str, Any],
    fraction: float,
    *,
    expected_surface_fingerprints: Sequence[str],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Rebuild one fraction from absolute cumulative-height endpoints.

    No candidate is derived from the previously evaluated candidate.  The
    growth=1 schedule and the authoritative physical schedule are combined by
    a binary64 convex combination at every cumulative layer height.
    """

    try:
        expected_binding = str(endpoint["baseline_binding_sha256"])
        expected_first = float(endpoint["first_layer_height_m"])
        expected_layers = int(endpoint["layer_count"])
    except (KeyError, TypeError, ValueError) as error:
        raise CoarseDirectionReplayError(
            "physical schedule homotopy endpoint is incomplete"
        ) from error
    validated_endpoint = validate_physical_schedule_homotopy_endpoint(
        endpoint,
        expected_binding_sha256=expected_binding,
        expected_first_layer_height_m=expected_first,
        expected_layer_count=expected_layers,
    )

    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise CoarseDirectionReplayError("homotopy fraction must be numeric")
    raw_fraction = _finite(fraction, "homotopy fraction")
    if raw_fraction not in validated_endpoint["fractions"]:
        raise CoarseDirectionReplayError(
            "homotopy fraction is not one of the frozen endpoint samples"
        )
    fingerprints = [str(item).casefold() for item in expected_surface_fingerprints]
    if (
        not fingerprints
        or len(fingerprints) != len(set(fingerprints))
        or any(_SHA256.fullmatch(item) is None for item in fingerprints)
    ):
        raise CoarseDirectionReplayError(
            "expected homotopy surface fingerprint inventory is invalid"
        )
    fingerprints.sort()
    physical = _normalize_expected_surface_schedules(
        baseline_surface_schedules,
        expected_surface_count=len(fingerprints),
        expected_layer_count=expected_layers,
    )
    if list(physical) != fingerprints:
        raise CoarseDirectionReplayError(
            "physical homotopy schedules differ from the stable wall inventory"
        )

    minimum_values = [
        expected_first * float(index) for index in range(1, expected_layers + 1)
    ]
    minimum = {fingerprint: list(minimum_values) for fingerprint in fingerprints}
    baseline_hash = _canonical_sha256({"surface_schedules": physical})
    minimum_hash = _canonical_sha256({"surface_schedules": minimum})
    candidate: dict[str, list[float]] = {}
    for fingerprint in fingerprints:
        values = physical[fingerprint]
        if values[0] != expected_first:
            raise CoarseDirectionReplayError(
                "one physical schedule does not preserve the exact first layer"
            )
        if any(
            value < minimum_value
            and not math.isclose(
                value, minimum_value, rel_tol=1.0e-12, abs_tol=1.0e-15
            )
            for value, minimum_value in zip(values, minimum_values)
        ):
            raise CoarseDirectionReplayError(
                "one physical schedule is below the fixed minimum-growth stack"
            )
        if raw_fraction == 0.0:
            interpolated = list(minimum_values)
        elif raw_fraction == 1.0:
            interpolated = list(values)
        else:
            interpolated = [
                minimum_value
                + raw_fraction * (physical_value - minimum_value)
                for minimum_value, physical_value in zip(minimum_values, values)
            ]
            # The shared first height is an invariant, not an approximate
            # consequence of floating-point interpolation.
            interpolated[0] = expected_first
        if (
            len(interpolated) != expected_layers
            or interpolated[0] != expected_first
            or any(not math.isfinite(item) or item <= 0.0 for item in interpolated)
            or any(right <= left for left, right in zip(interpolated, interpolated[1:]))
            or any(
                item < minimum_value
                or item > physical_value
                for item, minimum_value, physical_value in zip(
                    interpolated, minimum_values, values
                )
            )
        ):
            raise CoarseDirectionReplayError(
                "one interpolated physical schedule violates its convex invariants"
            )
        candidate[fingerprint] = interpolated

    if _canonical_sha256({"surface_schedules": physical}) != baseline_hash:
        raise CoarseDirectionReplayError(
            "baseline physical schedules changed during interpolation"
        )
    evidence: dict[str, Any] = {
        "schema": HOMOTOPY_APPLICATION_SCHEMA,
        "status": "PASS",
        "endpoint_sha256": validated_endpoint["endpoint_sha256"],
        "fraction_float_hex": raw_fraction.hex(),
        "interpolation_mode": HOMOTOPY_MODE,
        "baseline_surface_schedules_sha256": baseline_hash,
        "minimum_surface_schedules_sha256": minimum_hash,
        "candidate_surface_schedules_sha256": _canonical_sha256(
            {"surface_schedules": candidate}
        ),
        "surface_count": len(candidate),
        "layer_count": expected_layers,
        "first_layer_height_m": expected_first,
        "first_layer_height_preserved": all(
            values[0] == expected_first for values in candidate.values()
        ),
        "layer_count_preserved": all(
            len(values) == expected_layers for values in candidate.values()
        ),
        "candidate_schedules_strictly_increasing": all(
            all(right > left for left, right in zip(values, values[1:]))
            for values in candidate.values()
        ),
        "baseline_input_mutated": False,
    }
    evidence["application_sha256"] = _canonical_sha256(evidence)
    return candidate, evidence


def _argv_option(argv: list[str], option: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise CoarseDirectionReplayError(
            f"direction audit worker argv option {option!r} is missing or duplicated"
        )
    return argv[positions[0] + 1]


def validate_direction_replay_approval(
    value: Any,
    *,
    expected_coarse_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_local_schedule_binding_sha256: str,
    expected_projection_manifest_sha256: str,
    expected_projected_3d_elements: int,
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Validate the compact two-attestation replay contract in memory."""

    if not isinstance(value, Mapping):
        raise CoarseDirectionReplayError("direction replay approval is not an object")
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("approval_sha256", "")).casefold()
    expected_fields = {
        "schema",
        "status",
        "source_audit_manifests",
        "coarse_contract_sha256",
        "characteristic_length_m",
        "local_schedule_binding_sha256",
        "projection_manifest_sha256",
        "schedule_feasibility_endpoint",
        "direction_endpoint",
        "consensus",
        "source_minimum_growth_final_quality",
        "candidate_root_count",
        "changed_root_count",
        "root_contexts",
        "changed_roots",
        "runtime_mesh_tags_hardcoded",
        "audit_only_sources_preserved",
        "audit_only_direction_replay_authorized",
        "mesh_write_authorized",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
    }
    if (
        set(unsigned) != expected_fields
        or value.get("schema") != APPROVAL_SCHEMA
        or value.get("status") != "PASS"
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or value.get("audit_only_sources_preserved") is not True
        or value.get("audit_only_direction_replay_authorized") is not True
        or value.get("mesh_write_authorized") is not False
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
    ):
        raise CoarseDirectionReplayError("direction replay approval is stale or unsafe")

    sources = value.get("source_audit_manifests")
    source_fields = {
        "path",
        "sha256",
        "size_bytes",
        "schema",
        "status",
        "worker_process_id",
        "started_at_utc",
        "ended_at_utc",
    }
    if not isinstance(sources, list) or len(sources) != 2:
        raise CoarseDirectionReplayError(
            "direction replay requires two audit attestations"
        )
    source_identities: list[tuple[str, str, int]] = []
    for source in sources:
        if (
            not isinstance(source, Mapping)
            or set(source) != source_fields
            or not Path(str(source.get("path", ""))).is_absolute()
            or _SHA256.fullmatch(str(source.get("sha256", ""))) is None
            or isinstance(source.get("size_bytes"), bool)
            or not isinstance(source.get("size_bytes"), int)
            or source["size_bytes"] <= 0
            or source.get("schema") != AUDIT_MANIFEST_SCHEMA
            or source.get("status") != "PASS"
            or isinstance(source.get("worker_process_id"), bool)
            or not isinstance(source.get("worker_process_id"), int)
            or source["worker_process_id"] <= 0
            or not isinstance(source.get("started_at_utc"), str)
            or not isinstance(source.get("ended_at_utc"), str)
            or not source["started_at_utc"].endswith("Z")
            or not source["ended_at_utc"].endswith("Z")
        ):
            raise CoarseDirectionReplayError(
                "one direction replay source identity is invalid"
            )
        source_identities.append(
            (str(source["path"]), str(source["sha256"]), source["worker_process_id"])
        )
    if (
        sources != sorted(sources, key=lambda item: str(item["sha256"]))
        or len({item[0] for item in source_identities}) != 2
        or len({item[1] for item in source_identities}) != 2
        or len({item[2] for item in source_identities}) != 2
    ):
        raise CoarseDirectionReplayError(
            "direction replay attestations are not independent and sorted"
        )

    characteristic = _finite(
        value.get("characteristic_length_m"), "replay characteristic length"
    )
    expected_contract = str(expected_coarse_contract_sha256).casefold()
    expected_binding = str(expected_local_schedule_binding_sha256).casefold()
    expected_projection = str(expected_projection_manifest_sha256).casefold()
    if (
        any(_SHA256.fullmatch(raw) is None for raw in (
            expected_contract,
            expected_binding,
            expected_projection,
        ))
        or value.get("coarse_contract_sha256") != expected_contract
        or value.get("local_schedule_binding_sha256") != expected_binding
        or value.get("projection_manifest_sha256") != expected_projection
        or not math.isclose(
            characteristic,
            _finite(
                expected_characteristic_length_m,
                "expected replay characteristic length",
            ),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
    ):
        raise CoarseDirectionReplayError("direction replay lineage differs from the run")

    try:
        schedule_endpoint = validate_minimum_growth_endpoint(
            value.get("schedule_feasibility_endpoint"),
            expected_binding_sha256=expected_binding,
            expected_first_layer_height_m=float(
                value["schedule_feasibility_endpoint"]["first_layer_height_m"]
            ),
            expected_layer_count=int(
                value["schedule_feasibility_endpoint"]["layer_count"]
            ),
        )
        direction_endpoint = validate_owner_free_direction_endpoint(
            value.get("direction_endpoint"),
            expected_schedule_endpoint_sha256=str(
                schedule_endpoint["endpoint_sha256"]
            ),
        )
    except (
        CoarseScheduleFeasibilityError,
        CoarseDirectionFeasibilityError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise CoarseDirectionReplayError(
            f"direction replay endpoint lineage is invalid: {error}"
        ) from error

    consensus = value.get("consensus")
    consensus_fields = {
        "schema",
        "status",
        "attestation_count",
        "stable_observation_sha256",
        "schedule_endpoint_sha256",
        "direction_endpoint_sha256",
        "interaction_topology_sha256",
        "state_a_coordinate_sha256",
        "state_b_coordinate_sha256",
        "state_a_quality_sha256",
        "state_b_quality_sha256",
        "final_quality_sha256",
        "final_component_recheck_sha256",
        "final_low_quality_prisms_sha256",
        "changed_roots_sha256",
        "consensus_sha256",
    }
    if not isinstance(consensus, Mapping):
        raise CoarseDirectionReplayError("direction replay consensus is missing")
    consensus_unsigned = dict(consensus)
    consensus_hash = str(consensus_unsigned.pop("consensus_sha256", ""))
    digest_fields = consensus_fields - {
        "schema",
        "status",
        "attestation_count",
        "consensus_sha256",
    }
    if (
        set(consensus) != consensus_fields
        or consensus.get("schema")
        != "cfdpipe.coarse_direction_replay_consensus.v1"
        or consensus.get("status") != "PASS"
        or consensus.get("attestation_count") != 2
        or _SHA256.fullmatch(consensus_hash) is None
        or _canonical_sha256(consensus_unsigned) != consensus_hash
        or any(
            _SHA256.fullmatch(str(consensus.get(field, ""))) is None
            for field in digest_fields
        )
        or consensus.get("schedule_endpoint_sha256")
        != schedule_endpoint["endpoint_sha256"]
        or consensus.get("direction_endpoint_sha256")
        != direction_endpoint["endpoint_sha256"]
    ):
        raise CoarseDirectionReplayError("direction replay consensus is stale")

    final_quality = _validate_quality_snapshot(
        value.get("source_minimum_growth_final_quality"),
        label="direction replay source minimum-growth final quality",
        expected_minimum_prism_scaled_jacobian=(
            expected_minimum_prism_scaled_jacobian
        ),
        expected_minimum_core_tetra_gamma=expected_minimum_core_tetra_gamma,
        require_pass=True,
    )
    prism_count = final_quality["prism_element_count"]
    core_count = final_quality["core_element_count"]
    if (
        prism_count + core_count != expected_projected_3d_elements
        or final_quality["quality_sha256"]
        != consensus.get("final_quality_sha256")
    ):
        raise CoarseDirectionReplayError("direction replay final quality is not PASS")

    candidate_count = value.get("candidate_root_count")
    changed_count = value.get("changed_root_count")
    contexts = value.get("root_contexts")
    roots = value.get("changed_roots")
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count <= 0
        or isinstance(changed_count, bool)
        or not isinstance(changed_count, int)
        or changed_count <= 0
        or changed_count > candidate_count
        or not isinstance(contexts, list)
        or len(contexts) != candidate_count
        or not isinstance(roots, list)
        or len(roots) != changed_count
    ):
        raise CoarseDirectionReplayError("direction replay root counts are invalid")

    context_fields = {
        "root_coordinate_sha256",
        "incident_bad_source_triangle_sha256",
        "incident_wall_surface_fingerprints",
        "base_component_sha256",
        "interaction_component_sha256",
        "changed",
        "root_context_sha256",
    }
    context_ids: list[str] = []
    for context in contexts:
        if not isinstance(context, Mapping):
            raise CoarseDirectionReplayError("one replay root context is invalid")
        context_unsigned = dict(context)
        context_hash = str(context_unsigned.pop("root_context_sha256", ""))
        triangles = context.get("incident_bad_source_triangle_sha256")
        walls = context.get("incident_wall_surface_fingerprints")
        if (
            set(context) != context_fields
            or _SHA256.fullmatch(context_hash) is None
            or _canonical_sha256(context_unsigned) != context_hash
            or _SHA256.fullmatch(str(context.get("root_coordinate_sha256", "")))
            is None
            or _SHA256.fullmatch(str(context.get("base_component_sha256", "")))
            is None
            or _SHA256.fullmatch(
                str(context.get("interaction_component_sha256", ""))
            )
            is None
            or not isinstance(triangles, list)
            or not triangles
            or triangles != sorted(set(triangles))
            or any(_SHA256.fullmatch(str(item)) is None for item in triangles)
            or not isinstance(walls, list)
            or not walls
            or walls != sorted(set(walls))
            or any(_SHA256.fullmatch(str(item)) is None for item in walls)
            or not isinstance(context.get("changed"), bool)
        ):
            raise CoarseDirectionReplayError("one replay root context is stale")
        context_ids.append(str(context["root_coordinate_sha256"]))
    if context_ids != sorted(set(context_ids)):
        raise CoarseDirectionReplayError("replay root contexts are duplicated or unsorted")

    root_fields = {
        "root_coordinate_sha256",
        "root_context_sha256",
        "original_direction_float_hex",
        "selected_direction_float_hex",
        "selected_source",
        "selected_interior_weight_float_hex",
    }
    root_ids: list[str] = []
    context_by_id = {str(item["root_coordinate_sha256"]): item for item in contexts}
    for record in roots:
        root_id = str(record.get("root_coordinate_sha256", "")) if isinstance(record, Mapping) else ""
        context = context_by_id.get(root_id)
        if (
            not isinstance(record, Mapping)
            or set(record) != root_fields
            or context is None
            or context.get("changed") is not True
            or record.get("root_context_sha256")
            != context.get("root_context_sha256")
            or not isinstance(record.get("selected_source"), str)
            or not record["selected_source"]
            or not isinstance(record.get("selected_interior_weight_float_hex"), str)
        ):
            raise CoarseDirectionReplayError("one direction replay root is invalid")
        for field in ("original_direction_float_hex", "selected_direction_float_hex"):
            encoded = record.get(field)
            if not isinstance(encoded, list) or len(encoded) != 3:
                raise CoarseDirectionReplayError("one replay direction encoding is invalid")
            try:
                decoded = [float.fromhex(str(component)) for component in encoded]
            except ValueError as error:
                raise CoarseDirectionReplayError(
                    "one replay direction encoding is invalid"
                ) from error
            if _float_hex_vector(decoded, field) != encoded:
                raise CoarseDirectionReplayError(
                    "one replay direction encoding is non-canonical"
                )
        try:
            weight = float.fromhex(record["selected_interior_weight_float_hex"])
        except ValueError as error:
            raise CoarseDirectionReplayError(
                "one replay interior weight is invalid"
            ) from error
        if not math.isfinite(weight) or not 0.0 < weight <= 1.0:
            raise CoarseDirectionReplayError(
                "one replay interior weight is outside (0, 1]"
            )
        root_ids.append(root_id)
    if (
        root_ids != sorted(set(root_ids))
        or {item for item, context in context_by_id.items() if context["changed"]}
        != set(root_ids)
        or _canonical_sha256(roots) != consensus.get("changed_roots_sha256")
    ):
        raise CoarseDirectionReplayError("direction replay changed roots are inconsistent")
    return copy.deepcopy(dict(value))


def validate_physical_schedule_replay_discovery(
    value: Any,
    *,
    expected_approval: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_prism_element_count: int,
    expected_core_element_count: int,
    expected_layer_count: int,
    expected_surface_count: int,
    expected_surface_schedules: Mapping[str, Any],
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Validate a physical-schedule fixed-direction replay result."""

    if not isinstance(value, Mapping):
        raise CoarseDirectionReplayError(
            "physical-schedule replay discovery is not an object"
        )
    expected_fields = {
        "schema",
        "status",
        "profile_complete",
        "audit_only",
        "audit_variant",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_written",
        "su2_called",
        "paraview_called",
        "runtime_mesh_tags_hardcoded",
        "source_bindings",
        "fixed_direction_contract",
        "root_context_replay",
        "schedule_application",
        "coordinate_replay",
        "interaction_recheck",
        "final_quality",
        "final_low_quality_prisms",
        "observed_counts",
        "incomplete_reason",
        "discovery_sha256",
    }
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("discovery_sha256", ""))
    status = value.get("status")
    if (
        set(value) != expected_fields
        or value.get("schema") != PHYSICAL_DISCOVERY_SCHEMA
        or status not in {"PASS", "INCOMPLETE"}
        or value.get("profile_complete") is not (status == "PASS")
        or value.get("audit_only") is not True
        or value.get("audit_variant")
        != "physical_local_schedule_fixed_direction_replay"
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_written") is not False
        or value.get("su2_called") is not False
        or value.get("paraview_called") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
        or value.get("incomplete_reason")
        != (
            None
            if status == "PASS"
            else "PHYSICAL_LOCAL_SCHEDULE_FIXED_DIRECTION_REPLAY_QUALITY_FAILED"
        )
    ):
        raise CoarseDirectionReplayError(
            "physical-schedule replay discovery is stale or unsafe"
        )

    sources = value.get("source_bindings")
    if (
        not isinstance(sources, Mapping)
        or set(sources)
        != {
            "direction_replay_approval_sha256",
            "source_audit_manifest_sha256",
            "local_schedule_binding_sha256",
        }
        or sources.get("direction_replay_approval_sha256")
        != expected_approval.get("approval_sha256")
        or sources.get("local_schedule_binding_sha256")
        != expected_approval.get("local_schedule_binding_sha256")
        or sources.get("source_audit_manifest_sha256")
        != [
            record["sha256"]
            for record in expected_approval["source_audit_manifests"]
        ]
    ):
        raise CoarseDirectionReplayError(
            "physical-schedule replay source bindings differ"
        )

    contract = value.get("fixed_direction_contract")
    contract_fields = {
        "candidate_root_count",
        "changed_root_count",
        "unchanged_root_count",
        "direction_search_performed",
        "absolute_coordinate_replay",
        "original_direction_compatibility",
        "changed_roots",
    }
    if (
        not isinstance(contract, Mapping)
        or set(contract) != contract_fields
        or contract.get("candidate_root_count")
        != expected_approval.get("candidate_root_count")
        or contract.get("changed_root_count")
        != expected_approval.get("changed_root_count")
        or contract.get("unchanged_root_count")
        != expected_approval["candidate_root_count"]
        - expected_approval["changed_root_count"]
        or contract.get("direction_search_performed") is not False
        or contract.get("absolute_coordinate_replay") is not True
        or not isinstance(contract.get("changed_roots"), list)
        or len(contract["changed_roots"])
        != expected_approval["changed_root_count"]
    ):
        raise CoarseDirectionReplayError(
            "physical-schedule replay fixed-direction contract differs"
        )
    compatibility = contract.get("original_direction_compatibility")
    compatibility_fields = {
        "schema",
        "status",
        "absolute_tolerance_float_hex",
        "state_a_uses_fresh_runtime_direction",
        "selected_state_uses_consensus_direction",
        "changed_root_count",
        "exact_match_root_count",
        "roundoff_match_root_count",
        "records",
        "records_sha256",
        "compatibility_sha256",
    }
    compatibility_record_fields = {
        "root_coordinate_sha256",
        "runtime_original_direction_float_hex",
        "consensus_original_direction_float_hex",
        "maximum_absolute_difference_float_hex",
        "l2_difference_float_hex",
        "dot_product_float_hex",
        "matched_exactly",
        "within_absolute_tolerance",
    }
    if (
        not isinstance(compatibility, Mapping)
        or set(compatibility) != compatibility_fields
        or compatibility.get("schema")
        != "cfdpipe.coarse_direction_roundoff_compatibility.v1"
        or compatibility.get("status") != "PASS"
        or compatibility.get("absolute_tolerance_float_hex")
        != _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE.hex()
        or compatibility.get("state_a_uses_fresh_runtime_direction")
        is not True
        or compatibility.get("selected_state_uses_consensus_direction")
        is not True
        or compatibility.get("changed_root_count")
        != expected_approval["changed_root_count"]
        or not isinstance(compatibility.get("records"), list)
        or len(compatibility["records"])
        != expected_approval["changed_root_count"]
    ):
        raise CoarseDirectionReplayError(
            "physical replay original-direction compatibility is incomplete"
        )
    expected_changed_by_root = {
        str(record["root_coordinate_sha256"]): record
        for record in expected_approval["changed_roots"]
    }
    compatibility_root_ids: list[str] = []
    exact_match_count = 0
    for compatibility_record in compatibility["records"]:
        root_id = (
            str(compatibility_record.get("root_coordinate_sha256", ""))
            if isinstance(compatibility_record, Mapping)
            else ""
        )
        expected_changed = expected_changed_by_root.get(root_id)
        if (
            not isinstance(compatibility_record, Mapping)
            or set(compatibility_record) != compatibility_record_fields
            or expected_changed is None
            or compatibility_record.get(
                "consensus_original_direction_float_hex"
            )
            != expected_changed["original_direction_float_hex"]
        ):
            raise CoarseDirectionReplayError(
                "one physical replay direction compatibility record is stale"
            )
        runtime_direction = _decode_canonical_unit_hex(
            compatibility_record.get(
                "runtime_original_direction_float_hex"
            ),
            "runtime replay original direction",
        )
        consensus_direction = _decode_canonical_unit_hex(
            compatibility_record.get(
                "consensus_original_direction_float_hex"
            ),
            "consensus replay original direction",
        )
        deltas = [
            runtime - consensus
            for runtime, consensus in zip(
                runtime_direction, consensus_direction
            )
        ]
        maximum_difference = max(abs(value) for value in deltas)
        l2_difference = math.sqrt(math.fsum(value * value for value in deltas))
        dot_product = math.fsum(
            runtime * consensus
            for runtime, consensus in zip(
                runtime_direction, consensus_direction
            )
        )
        matched_exactly = (
            compatibility_record["runtime_original_direction_float_hex"]
            == compatibility_record["consensus_original_direction_float_hex"]
        )
        if (
            compatibility_record.get(
                "maximum_absolute_difference_float_hex"
            )
            != maximum_difference.hex()
            or compatibility_record.get("l2_difference_float_hex")
            != l2_difference.hex()
            or compatibility_record.get("dot_product_float_hex")
            != dot_product.hex()
            or compatibility_record.get("matched_exactly")
            is not matched_exactly
            or compatibility_record.get("within_absolute_tolerance")
            is not True
            or maximum_difference
            > _DIRECTION_REPLAY_ORIGINAL_ABSOLUTE_TOLERANCE
        ):
            raise CoarseDirectionReplayError(
                "one physical replay direction exceeds binary64 roundoff"
            )
        exact_match_count += int(matched_exactly)
        compatibility_root_ids.append(root_id)
    if (
        compatibility_root_ids
        != sorted(expected_changed_by_root)
        or compatibility.get("exact_match_root_count") != exact_match_count
        or compatibility.get("roundoff_match_root_count")
        != len(compatibility_root_ids) - exact_match_count
        or compatibility.get("records_sha256")
        != _canonical_sha256({"records": compatibility["records"]})
    ):
        raise CoarseDirectionReplayError(
            "physical replay direction compatibility summary differs"
        )
    unsigned_compatibility = dict(compatibility)
    configured_compatibility_sha = str(
        unsigned_compatibility.pop("compatibility_sha256", "")
    ).casefold()
    if (
        _SHA256.fullmatch(configured_compatibility_sha) is None
        or _canonical_sha256(unsigned_compatibility)
        != configured_compatibility_sha
    ):
        raise CoarseDirectionReplayError(
            "physical replay direction compatibility hash is stale"
        )
    expected_changed_records = [
        {
            "root_coordinate_sha256": record["root_coordinate_sha256"],
            "root_context_sha256": record["root_context_sha256"],
            "original_direction_float_hex": record[
                "original_direction_float_hex"
            ],
            "selected_direction_float_hex": record[
                "selected_direction_float_hex"
            ],
            "application": (
                "ABSOLUTE_ORIGIN_PLUS_DIRECTION_TIMES_REAL_SCHEDULE"
            ),
        }
        for record in expected_approval["changed_roots"]
    ]
    changed_root_ids = [
        str(record.get("root_coordinate_sha256", ""))
        for record in contract["changed_roots"]
        if isinstance(record, Mapping)
    ]
    if (
        any(
            not isinstance(record, Mapping)
            or set(record)
            != {
                "root_coordinate_sha256",
                "root_context_sha256",
                "original_direction_float_hex",
                "selected_direction_float_hex",
                "application",
            }
            for record in contract["changed_roots"]
        )
        or changed_root_ids != sorted(set(changed_root_ids))
        or contract["changed_roots"] != expected_changed_records
    ):
        raise CoarseDirectionReplayError(
            "physical replay changed roots are not a sorted, unique, exact "
            "consensus projection"
        )

    contexts = value.get("root_context_replay")
    if (
        not isinstance(contexts, Mapping)
        or set(contexts)
        != {
            "expected_count",
            "matched_count",
            "missing_count",
            "duplicate_count",
            "unexplained_count",
            "contexts",
        }
        or contexts.get("expected_count")
        != expected_approval["candidate_root_count"]
        or contexts.get("matched_count")
        != expected_approval["candidate_root_count"]
        or any(
            contexts.get(field) != 0
            for field in ("missing_count", "duplicate_count", "unexplained_count")
        )
        or contexts.get("contexts") != expected_approval["root_contexts"]
    ):
        raise CoarseDirectionReplayError(
            "physical replay root-context matching is incomplete"
        )

    normalized_surface_schedules = _normalize_expected_surface_schedules(
        expected_surface_schedules,
        expected_surface_count=expected_surface_count,
        expected_layer_count=expected_layer_count,
    )
    baseline_schedule_sha256 = _canonical_sha256(
        {"surface_schedules": normalized_surface_schedules}
    )
    surface_totals = [
        schedule_values[-1]
        for schedule_values in normalized_surface_schedules.values()
    ]
    schedule = value.get("schedule_application")
    reported_root_assignments = (
        schedule.get("root_schedule_assignments")
        if isinstance(schedule, Mapping)
        else None
    )
    if not isinstance(reported_root_assignments, list):
        raise CoarseDirectionReplayError(
            "physical replay root schedule assignments are missing"
        )
    reported_assignment_by_root: dict[str, Mapping[str, Any]] = {}
    assignment_fields = {
        "root_coordinate_sha256",
        "incident_wall_surface_fingerprints",
        "cumulative_heights_sha256",
        "selected_total_thickness_float_hex",
    }
    for record in reported_root_assignments:
        root_id = (
            str(record.get("root_coordinate_sha256", ""))
            if isinstance(record, Mapping)
            else ""
        )
        if (
            not isinstance(record, Mapping)
            or set(record) != assignment_fields
            or _SHA256.fullmatch(root_id) is None
            or root_id in reported_assignment_by_root
        ):
            raise CoarseDirectionReplayError(
                "one physical replay root schedule assignment is invalid"
            )
        reported_assignment_by_root[root_id] = record
    expected_root_assignments: list[dict[str, Any]] = []
    for context in expected_approval["root_contexts"]:
        root_id = str(context["root_coordinate_sha256"])
        reported = reported_assignment_by_root.get(root_id)
        if reported is None:
            raise CoarseDirectionReplayError(
                "one approved root lacks a physical schedule assignment"
            )
        incident_surfaces = [
            str(item).casefold()
            for item in reported["incident_wall_surface_fingerprints"]
        ]
        approved_incident_surfaces = {
            str(item).casefold()
            for item in context["incident_wall_surface_fingerprints"]
        }
        if (
            incident_surfaces != sorted(set(incident_surfaces))
            or not approved_incident_surfaces <= set(incident_surfaces)
            or any(
                fingerprint not in normalized_surface_schedules
                for fingerprint in incident_surfaces
            )
        ):
            raise CoarseDirectionReplayError(
                "approval root context references an unavailable surface schedule"
            )
        cumulative = [
            min(
                normalized_surface_schedules[fingerprint][layer]
                for fingerprint in incident_surfaces
            )
            for layer in range(expected_layer_count)
        ]
        if any(right <= left for left, right in zip(cumulative, cumulative[1:])):
            raise CoarseDirectionReplayError(
                "approval root element-wise-minimum schedule is not monotone"
            )
        expected_root_assignments.append(
            {
                "root_coordinate_sha256": root_id,
                "incident_wall_surface_fingerprints": incident_surfaces,
                "cumulative_heights_sha256": _canonical_sha256(
                    {
                        "cumulative_heights_float_hex": [
                            item.hex() for item in cumulative
                        ]
                    }
                ),
                "selected_total_thickness_float_hex": cumulative[-1].hex(),
            }
        )
    expected_root_assignments.sort(
        key=lambda record: record["root_coordinate_sha256"]
    )
    if (
        set(reported_assignment_by_root)
        != {
            str(context["root_coordinate_sha256"])
            for context in expected_approval["root_contexts"]
        }
        or reported_root_assignments != expected_root_assignments
    ):
        raise CoarseDirectionReplayError(
            "physical replay root schedule assignments are not canonical"
        )
    expected_assignment_sha256 = _canonical_sha256(
        {"root_schedule_assignments": expected_root_assignments}
    )

    if (
        not isinstance(schedule, Mapping)
        or set(schedule)
        != {
            "minimum_growth_schedule_used",
            "surface_count",
            "layer_count",
            "baseline_surface_schedules_sha256",
            "minimum_surface_total_thickness_m",
            "maximum_surface_total_thickness_m",
            "root_schedule_assignment_count",
            "root_schedule_assignments",
            "root_schedule_assignments_sha256",
        }
        or schedule.get("minimum_growth_schedule_used") is not False
        or schedule.get("surface_count") != expected_surface_count
        or schedule.get("layer_count") != expected_layer_count
        or schedule.get("baseline_surface_schedules_sha256")
        != baseline_schedule_sha256
        or _finite(
            schedule.get("minimum_surface_total_thickness_m"),
            "minimum physical schedule thickness",
        ).hex()
        != min(surface_totals).hex()
        or _finite(
            schedule.get("maximum_surface_total_thickness_m"),
            "maximum physical schedule thickness",
        ).hex()
        != max(surface_totals).hex()
        or schedule.get("root_schedule_assignment_count")
        != expected_approval["candidate_root_count"]
        or schedule.get("root_schedule_assignments")
        != expected_root_assignments
        or schedule.get("root_schedule_assignments_sha256")
        != expected_assignment_sha256
    ):
        raise CoarseDirectionReplayError(
            "physical replay did not consume the real local schedule"
        )

    coordinate = value.get("coordinate_replay")
    coordinate_hash_fields = (
        "state_a_first_coordinate_sha256",
        "state_b_first_coordinate_sha256",
        "state_a_second_coordinate_sha256",
        "state_b_second_coordinate_sha256",
        "state_a_first_quality_sha256",
        "state_b_first_quality_sha256",
        "state_a_second_quality_sha256",
        "state_b_second_quality_sha256",
        "non_target_coordinate_sha256",
    )
    if (
        not isinstance(coordinate, Mapping)
        or set(coordinate)
        != {
            "encoding",
            *coordinate_hash_fields,
            "state_a_reproducible",
            "state_b_reproducible",
            "a_b_distinct",
            "base_root_moved_count",
            "unchanged_chain_changed_count",
            "non_target_node_changed_count",
        }
        or coordinate.get("encoding")
        != "python-float-hex/root-sha/physical-layer-v1"
        or any(
            _SHA256.fullmatch(str(coordinate.get(field, ""))) is None
            for field in coordinate_hash_fields
        )
        or coordinate.get("state_a_first_coordinate_sha256")
        != coordinate.get("state_a_second_coordinate_sha256")
        or coordinate.get("state_b_first_coordinate_sha256")
        != coordinate.get("state_b_second_coordinate_sha256")
        or coordinate.get("state_a_first_coordinate_sha256")
        == coordinate.get("state_b_first_coordinate_sha256")
        or coordinate.get("state_a_first_quality_sha256")
        != coordinate.get("state_a_second_quality_sha256")
        or coordinate.get("state_b_first_quality_sha256")
        != coordinate.get("state_b_second_quality_sha256")
        or coordinate.get("state_a_reproducible") is not True
        or coordinate.get("state_b_reproducible") is not True
        or coordinate.get("a_b_distinct") is not True
        or any(
            coordinate.get(field) != 0
            for field in (
                "base_root_moved_count",
                "unchanged_chain_changed_count",
                "non_target_node_changed_count",
            )
        )
    ):
        raise CoarseDirectionReplayError(
            "physical replay A-B-A-B coordinate evidence is invalid"
        )

    counts = value.get("observed_counts")
    count_fields = {
        "projected_3d_element_count",
        "prism_element_count",
        "core_element_count",
        "core_tetra_count",
        "candidate_root_count",
        "changed_root_count",
        "unchanged_root_count",
        "applied_direction_count",
        "searched_direction_count",
    }
    if (
        not isinstance(counts, Mapping)
        or set(counts) != count_fields
        or any(
            isinstance(counts.get(field), bool)
            or not isinstance(counts.get(field), int)
            or counts[field] < 0
            for field in count_fields
        )
        or counts.get("projected_3d_element_count")
        != expected_projected_3d_elements
        or counts.get("prism_element_count") != expected_prism_element_count
        or counts.get("core_element_count") != expected_core_element_count
        or counts.get("core_tetra_count") != expected_core_element_count
        or counts.get("projected_3d_element_count")
        != counts.get("prism_element_count") + counts.get("core_element_count")
        or counts.get("candidate_root_count")
        != expected_approval["candidate_root_count"]
        or counts.get("changed_root_count")
        != expected_approval["changed_root_count"]
        or counts.get("unchanged_root_count")
        != expected_approval["candidate_root_count"]
        - expected_approval["changed_root_count"]
        or counts.get("applied_direction_count")
        != expected_approval["changed_root_count"]
        or counts.get("searched_direction_count") != 0
    ):
        raise CoarseDirectionReplayError(
            "physical replay global quality/count evidence is invalid"
        )
    final_quality = _validate_quality_snapshot(
        value.get("final_quality"),
        label="physical replay final quality",
        expected_minimum_prism_scaled_jacobian=(
            expected_minimum_prism_scaled_jacobian
        ),
        expected_minimum_core_tetra_gamma=expected_minimum_core_tetra_gamma,
        expected_prism_element_count=expected_prism_element_count,
        expected_core_element_count=expected_core_element_count,
        require_pass=status == "PASS",
    )
    if (
        final_quality["quality_sha256"]
        != coordinate.get("state_b_second_quality_sha256")
        or final_quality["status"] != ("PASS" if status == "PASS" else "FAIL")
    ):
        raise CoarseDirectionReplayError(
            "physical replay final quality does not match its replay state"
        )

    low = value.get("final_low_quality_prisms")
    recheck = value.get("interaction_recheck")
    if (
        not isinstance(low, Mapping)
        or set(low) != {"count", "records", "records_sha256"}
        or not isinstance(low.get("records"), list)
        or low.get("count") != len(low["records"])
        or low.get("records_sha256")
        != _canonical_sha256({"records": low["records"]})
        or low.get("count")
        != final_quality["prism_below_threshold_element_count"]
        or (status == "PASS" and low.get("count") != 0)
        or not isinstance(recheck, list)
        or not recheck
    ):
        raise CoarseDirectionReplayError(
            "physical replay residual/component evidence is invalid"
        )

    expected_component_roots: dict[str, set[str]] = {}
    for context in expected_approval["root_contexts"]:
        component_id = str(context["interaction_component_sha256"])
        expected_component_roots.setdefault(component_id, set()).add(
            str(context["root_coordinate_sha256"])
        )
    component_ids: list[str] = []
    affected_prism_total = 0
    affected_core_total = 0
    for record in recheck:
        if not isinstance(record, Mapping) or set(record) != {
            "interaction_component_sha256",
            "candidate_root_count",
            "affected_prism_count",
            "affected_core_element_count",
            "affected_core_tetra_count",
            "quality",
        }:
            raise CoarseDirectionReplayError(
                "one physical replay interaction recheck has an incomplete field set"
            )
        component_id = str(record.get("interaction_component_sha256", ""))
        expected_roots = expected_component_roots.get(component_id)
        numeric_fields = (
            "candidate_root_count",
            "affected_prism_count",
            "affected_core_element_count",
            "affected_core_tetra_count",
        )
        if (
            expected_roots is None
            or any(
                isinstance(record.get(field), bool)
                or not isinstance(record.get(field), int)
                or record[field] <= 0
                for field in numeric_fields
            )
            or record["candidate_root_count"] != len(expected_roots)
            or record["affected_core_tetra_count"]
            != record["affected_core_element_count"]
        ):
            raise CoarseDirectionReplayError(
                "one physical replay interaction component has inconsistent counts"
            )
        component_quality = _validate_quality_snapshot(
            record["quality"],
            label=f"physical replay interaction {component_id}",
            expected_minimum_prism_scaled_jacobian=(
                expected_minimum_prism_scaled_jacobian
            ),
            expected_minimum_core_tetra_gamma=expected_minimum_core_tetra_gamma,
            expected_prism_element_count=record["affected_prism_count"],
            expected_core_element_count=record["affected_core_element_count"],
            require_pass=status == "PASS",
        )
        if status == "PASS" and component_quality["status"] != "PASS":
            raise CoarseDirectionReplayError(
                "one physical replay PASS component did not pass its quality gate"
            )
        component_ids.append(component_id)
        affected_prism_total += record["affected_prism_count"]
        affected_core_total += record["affected_core_element_count"]
    if (
        component_ids != sorted(set(component_ids))
        or set(component_ids) != set(expected_component_roots)
        or sum(len(roots) for roots in expected_component_roots.values())
        != expected_approval["candidate_root_count"]
        or affected_prism_total > expected_prism_element_count
        or affected_core_total > expected_core_element_count
    ):
        raise CoarseDirectionReplayError(
            "physical replay interaction component coverage is incomplete or duplicated"
        )
    return copy.deepcopy(dict(value))


def validate_physical_schedule_homotopy_discovery(
    value: Any,
    *,
    expected_endpoint: Mapping[str, Any],
    expected_approval: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_prism_element_count: int,
    expected_core_element_count: int,
    expected_layer_count: int,
    expected_surface_count: int,
    expected_surface_schedules: Mapping[str, Sequence[float]],
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Validate a complete ascending/reverse fixed-direction homotopy audit.

    Scientific quality failure is represented by a validated ``INCOMPLETE``
    result.  Missing fractions, inconsistent reverse replay, stale hashes,
    changed permissions, or any other evidence-integrity failure raises.
    """

    if not isinstance(value, Mapping):
        raise CoarseDirectionReplayError(
            "physical schedule homotopy discovery is not an object"
        )
    expected_fields = {
        "schema",
        "status",
        "profile_complete",
        "audit_only",
        "audit_variant",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_written",
        "su2_called",
        "paraview_called",
        "runtime_mesh_tags_hardcoded",
        "source_bindings",
        "fixed_direction_contract",
        "root_context_replay",
        "homotopy_contract",
        "ascending_scan",
        "descending_replay",
        "observed_counts",
        "final_quality",
        "maximum_tested_pass_fraction_float_hex",
        "first_tested_fail_fraction_float_hex",
        "incomplete_reason",
        "discovery_sha256",
    }
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("discovery_sha256", "")).casefold()
    status = value.get("status")
    if (
        set(value) != expected_fields
        or value.get("schema") != HOMOTOPY_DISCOVERY_SCHEMA
        or status not in {"PASS", "INCOMPLETE"}
        or value.get("profile_complete") is not (status == "PASS")
        or value.get("audit_only") is not True
        or value.get("audit_variant") != HOMOTOPY_AUDIT_VARIANT
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_written") is not False
        or value.get("su2_called") is not False
        or value.get("paraview_called") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
    ):
        raise CoarseDirectionReplayError(
            "physical schedule homotopy discovery is stale or unsafe"
        )

    if not isinstance(expected_approval, Mapping):
        raise CoarseDirectionReplayError("homotopy replay approval is missing")
    approval_unsigned = dict(expected_approval)
    approval_hash = str(approval_unsigned.pop("approval_sha256", "")).casefold()
    if (
        expected_approval.get("schema") != APPROVAL_SCHEMA
        or expected_approval.get("status") != "PASS"
        or _SHA256.fullmatch(approval_hash) is None
        or _canonical_sha256(approval_unsigned) != approval_hash
    ):
        raise CoarseDirectionReplayError("homotopy replay approval is stale")
    try:
        binding_hash = str(expected_approval["local_schedule_binding_sha256"])
        source_manifest_hashes = [
            str(record["sha256"])
            for record in expected_approval["source_audit_manifests"]
        ]
    except (KeyError, TypeError) as error:
        raise CoarseDirectionReplayError(
            "homotopy replay approval lineage is incomplete"
        ) from error
    endpoint = validate_physical_schedule_homotopy_endpoint(
        expected_endpoint,
        expected_binding_sha256=binding_hash,
        expected_first_layer_height_m=float(
            expected_endpoint.get("first_layer_height_m", float("nan"))
        ),
        expected_layer_count=expected_layer_count,
    )
    if endpoint["layer_count"] != expected_layer_count:
        raise CoarseDirectionReplayError(
            "homotopy endpoint layer count differs from the run"
        )

    sources = value.get("source_bindings")
    if (
        not isinstance(sources, Mapping)
        or set(sources)
        != {
            "direction_replay_approval_sha256",
            "source_audit_manifest_sha256",
            "local_schedule_binding_sha256",
            "homotopy_endpoint_sha256",
        }
        or sources.get("direction_replay_approval_sha256") != approval_hash
        or sources.get("source_audit_manifest_sha256") != source_manifest_hashes
        or sources.get("local_schedule_binding_sha256") != binding_hash
        or sources.get("homotopy_endpoint_sha256")
        != endpoint["endpoint_sha256"]
    ):
        raise CoarseDirectionReplayError(
            "physical homotopy source bindings differ from the approval"
        )

    fixed = value.get("fixed_direction_contract")
    fixed_required = {
        "candidate_root_count",
        "changed_root_count",
        "unchanged_root_count",
        "direction_search_performed",
        "absolute_coordinate_replay",
        "changed_roots",
    }
    if not isinstance(fixed, Mapping) or not fixed_required <= set(fixed):
        raise CoarseDirectionReplayError(
            "physical homotopy fixed-direction contract is incomplete"
        )
    candidate_count = expected_approval.get("candidate_root_count")
    changed_count = expected_approval.get("changed_root_count")
    if (
        isinstance(candidate_count, bool)
        or not isinstance(candidate_count, int)
        or candidate_count <= 0
        or isinstance(changed_count, bool)
        or not isinstance(changed_count, int)
        or changed_count <= 0
        or changed_count > candidate_count
        or fixed.get("candidate_root_count") != candidate_count
        or fixed.get("changed_root_count") != changed_count
        or fixed.get("unchanged_root_count") != candidate_count - changed_count
        or fixed.get("direction_search_performed") is not False
        or fixed.get("absolute_coordinate_replay") is not True
        or not isinstance(fixed.get("changed_roots"), list)
    ):
        raise CoarseDirectionReplayError(
            "physical homotopy changed the fixed-direction authorization"
        )
    approved_changed_ids = sorted(
        str(record["root_coordinate_sha256"])
        for record in expected_approval.get("changed_roots", [])
    )
    observed_changed_ids = sorted(
        str(record.get("root_coordinate_sha256", ""))
        for record in fixed["changed_roots"]
        if isinstance(record, Mapping)
    )
    if observed_changed_ids != approved_changed_ids:
        raise CoarseDirectionReplayError(
            "physical homotopy changed-root identity differs from the approval"
        )

    contexts = value.get("root_context_replay")
    if (
        not isinstance(contexts, Mapping)
        or contexts.get("expected_count") != candidate_count
        or contexts.get("matched_count") != candidate_count
        or any(
            contexts.get(field) != 0
            for field in ("missing_count", "duplicate_count", "unexplained_count")
        )
        or contexts.get("contexts") != expected_approval.get("root_contexts")
    ):
        raise CoarseDirectionReplayError(
            "physical homotopy root-context replay is incomplete"
        )

    normalized_schedules = _normalize_expected_surface_schedules(
        expected_surface_schedules,
        expected_surface_count=expected_surface_count,
        expected_layer_count=expected_layer_count,
    )
    fingerprints = list(normalized_schedules)
    baseline_schedule_hash = _canonical_sha256(
        {"surface_schedules": normalized_schedules}
    )
    _, minimum_application = interpolate_physical_schedule_homotopy(
        normalized_schedules,
        endpoint,
        0.0,
        expected_surface_fingerprints=fingerprints,
    )
    fractions = list(endpoint["fractions"])
    ascending_hex = [item.hex() for item in fractions]
    descending_hex = list(reversed(ascending_hex))
    homotopy = value.get("homotopy_contract")
    expected_homotopy = {
        "endpoint": endpoint,
        "baseline_surface_schedules_sha256": baseline_schedule_hash,
        "minimum_surface_schedules_sha256": minimum_application[
            "minimum_surface_schedules_sha256"
        ],
        "fraction_count": len(fractions),
        "ascending_fraction_float_hex": ascending_hex,
        "descending_fraction_float_hex": descending_hex,
        "absolute_schedule_rebuild": True,
        "ascending_descending_replay_required": True,
        "quality_monotonicity_assumed": False,
        "direction_search_performed": False,
    }
    if not isinstance(homotopy, Mapping) or dict(homotopy) != expected_homotopy:
        raise CoarseDirectionReplayError(
            "physical homotopy contract is stale or incomplete"
        )

    record_fields = {
        "fraction",
        "fraction_float_hex",
        "interpolation_evidence",
        "root_schedule_sha256",
        "coordinate_sha256",
        "quality",
        "low_quality_aggregate",
    }

    def validate_scan(
        raw_records: Any,
        expected_fractions: Sequence[float],
        label: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_records, list) or len(raw_records) != len(
            expected_fractions
        ):
            raise CoarseDirectionReplayError(
                f"physical homotopy {label} fraction coverage is incomplete"
            )
        validated: list[dict[str, Any]] = []
        for index, (record, expected_fraction) in enumerate(
            zip(raw_records, expected_fractions)
        ):
            if not isinstance(record, Mapping) or set(record) != record_fields:
                raise CoarseDirectionReplayError(
                    f"physical homotopy {label} record {index} is incomplete"
                )
            actual_fraction = _finite(
                record.get("fraction"), f"physical homotopy {label} fraction"
            )
            _, expected_application = interpolate_physical_schedule_homotopy(
                normalized_schedules,
                endpoint,
                expected_fraction,
                expected_surface_fingerprints=fingerprints,
            )
            if (
                actual_fraction != expected_fraction
                or record.get("fraction_float_hex") != expected_fraction.hex()
                or record.get("interpolation_evidence") != expected_application
                or _SHA256.fullmatch(str(record.get("root_schedule_sha256", "")))
                is None
                or _SHA256.fullmatch(str(record.get("coordinate_sha256", "")))
                is None
                or not isinstance(record.get("low_quality_aggregate"), Mapping)
            ):
                raise CoarseDirectionReplayError(
                    f"physical homotopy {label} record {index} is stale"
                )
            # This also rejects non-finite or non-JSON aggregate data.
            _canonical_sha256(record["low_quality_aggregate"])
            quality = _validate_quality_snapshot(
                record.get("quality"),
                label=f"physical homotopy {label} fraction {expected_fraction.hex()}",
                expected_minimum_prism_scaled_jacobian=(
                    expected_minimum_prism_scaled_jacobian
                ),
                expected_minimum_core_tetra_gamma=(
                    expected_minimum_core_tetra_gamma
                ),
                expected_prism_element_count=expected_prism_element_count,
                expected_core_element_count=expected_core_element_count,
            )
            validated.append({**copy.deepcopy(dict(record)), "quality": quality})
        return validated

    ascending = validate_scan(value.get("ascending_scan"), fractions, "ascending")
    descending = validate_scan(
        value.get("descending_replay"), list(reversed(fractions)), "descending"
    )
    ascending_by_fraction = {
        record["fraction_float_hex"]: record for record in ascending
    }
    for reverse_record in descending:
        forward_record = ascending_by_fraction.get(
            reverse_record["fraction_float_hex"]
        )
        if forward_record is None or any(
            reverse_record[field] != forward_record[field]
            for field in (
                "fraction",
                "fraction_float_hex",
                "interpolation_evidence",
                "root_schedule_sha256",
                "coordinate_sha256",
                "quality",
                "low_quality_aggregate",
            )
        ):
            raise CoarseDirectionReplayError(
                "physical homotopy reverse replay differs from its absolute "
                "ascending state"
            )

    source_minimum_quality = _validate_quality_snapshot(
        expected_approval.get("source_minimum_growth_final_quality"),
        label="homotopy approved minimum-growth quality",
        expected_minimum_prism_scaled_jacobian=(
            expected_minimum_prism_scaled_jacobian
        ),
        expected_minimum_core_tetra_gamma=expected_minimum_core_tetra_gamma,
        expected_prism_element_count=expected_prism_element_count,
        expected_core_element_count=expected_core_element_count,
        require_pass=True,
    )
    if ascending[0]["quality"] != source_minimum_quality:
        raise CoarseDirectionReplayError(
            "homotopy fraction zero does not reproduce the approved minimum-growth state"
        )

    counts = value.get("observed_counts")
    expected_counts = {
        "projected_3d_element_count": expected_projected_3d_elements,
        "prism_element_count": expected_prism_element_count,
        "core_element_count": expected_core_element_count,
        "core_tetra_count": expected_core_element_count,
        "homotopy_fraction_count": len(fractions),
        "ascending_quality_evaluation_count": len(fractions),
        "descending_quality_evaluation_count": len(fractions),
        "searched_direction_count": 0,
    }
    if (
        expected_projected_3d_elements
        != expected_prism_element_count + expected_core_element_count
        or not isinstance(counts, Mapping)
        or dict(counts) != expected_counts
    ):
        raise CoarseDirectionReplayError(
            "physical homotopy global counts are inconsistent"
        )

    all_pass = all(record["quality"]["status"] == "PASS" for record in ascending)
    computed_status = "PASS" if all_pass else "INCOMPLETE"
    pass_fractions = [
        float(record["fraction"])
        for record in ascending
        if record["quality"]["status"] == "PASS"
    ]
    fail_fractions = [
        float(record["fraction"])
        for record in ascending
        if record["quality"]["status"] != "PASS"
    ]
    expected_maximum_pass = max(pass_fractions).hex() if pass_fractions else None
    expected_first_fail = min(fail_fractions).hex() if fail_fractions else None
    if (
        status != computed_status
        or value.get("incomplete_reason")
        != (None if all_pass else HOMOTOPY_INCOMPLETE_REASON)
        or value.get("maximum_tested_pass_fraction_float_hex")
        != expected_maximum_pass
        or value.get("first_tested_fail_fraction_float_hex")
        != expected_first_fail
        or value.get("final_quality") != ascending[-1]["quality"]
    ):
        raise CoarseDirectionReplayError(
            "physical homotopy PASS/INCOMPLETE summary is inconsistent"
        )
    return copy.deepcopy(dict(value))


def _load_direction_audit_attestation(
    manifest_path: str | os.PathLike[str],
    manifest_sha256: str,
    *,
    expected_audit_strategy: Mapping[str, Any],
    expected_coarse_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_local_schedule_binding: Mapping[str, Any],
    expected_projection_evidence: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_worker_memory_limit_bytes: int,
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Load and authoritatively validate one immutable PASS audit."""

    raw = Path(manifest_path).expanduser()
    if ".." in raw.parts:
        raise CoarseDirectionReplayError("direction audit path must not contain '..'")
    path = Path(os.path.abspath(raw)).resolve(strict=True)
    _reject_link_components(path)
    expected_sha = str(manifest_sha256).casefold()
    if (
        not path.is_file()
        or path.name != "coarse_repair_audit_manifest.json"
        or _SHA256.fullmatch(expected_sha) is None
        or _sha256_file(path) != expected_sha
    ):
        raise CoarseDirectionReplayError("direction audit file identity is invalid")
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseDirectionReplayError("cannot parse direction audit manifest") from error
    if not isinstance(manifest, Mapping):
        raise CoarseDirectionReplayError("direction audit manifest is not an object")

    source_before = manifest.get("source_before")
    source_after = manifest.get("source_after")
    session = manifest.get("gmsh_session")
    isolation = manifest.get("worker_isolation")
    if (
        manifest.get("schema") != AUDIT_MANIFEST_SCHEMA
        or manifest.get("status") != "PASS"
        or manifest.get("audit_only") is not True
        or manifest.get("audit_purpose") != PATTERN_PURPOSE
        or manifest.get("schedule_feasibility_endpoint_requested") != PATTERN_REQUEST
        or manifest.get("calibration_PASS_authorized") is not False
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("mesh_written") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
        or manifest.get("external_commands") != []
        or manifest.get("source_unchanged") is not True
        or not isinstance(source_before, Mapping)
        or source_before != source_after
        or source_before.get("read_only") is not True
        or not isinstance(session, Mapping)
        or session.get("initialize_called") is not True
        or session.get("finalize_called") is not True
        or session.get("cleanup_errors") != []
        or session.get("logger_capture_succeeded") is not True
        or session.get("diagnostic_audit", {}).get("status") != "PASS"
        or not isinstance(isolation, Mapping)
        or isolation.get("schema") != "cfdpipe.coarse_repair_audit_worker.v1"
        or isolation.get("status") != "PASS"
        or isolation.get("audit_result_status") != "PASS"
        or isolation.get("audit_only") is not True
        or isolation.get("mesh_written") is not False
        or isolation.get("su2_called") is not False
        or isolation.get("paraview_called") is not False
    ):
        raise CoarseDirectionReplayError(
            "direction audit manifest violates its immutable audit-only scope"
        )

    if (
        isinstance(expected_worker_memory_limit_bytes, bool)
        or not isinstance(expected_worker_memory_limit_bytes, int)
        or expected_worker_memory_limit_bytes <= 0
    ):
        raise CoarseDirectionReplayError(
            "expected direction audit worker memory limit is invalid"
        )
    run_evidence = manifest.get("run_evidence")
    worker = isolation.get("worker")
    worker_memory = isolation.get("worker_memory")
    hard_limit = (
        worker_memory.get("hard_limit")
        if isinstance(worker_memory, Mapping)
        else None
    )
    job_object = (
        hard_limit.get("job_object")
        if isinstance(hard_limit, Mapping)
        else None
    )
    process_after_limit = (
        worker_memory.get("process_after_limit")
        if isinstance(worker_memory, Mapping)
        else None
    )
    process_after_audit = (
        worker_memory.get("process_after_audit")
        if isinstance(worker_memory, Mapping)
        else None
    )
    argv = worker.get("argv") if isinstance(worker, Mapping) else None
    if (
        not isinstance(run_evidence, Mapping)
        or not isinstance(worker, Mapping)
        or not isinstance(worker_memory, Mapping)
        or not isinstance(hard_limit, Mapping)
        or not isinstance(job_object, Mapping)
        or not isinstance(process_after_limit, Mapping)
        or not isinstance(process_after_audit, Mapping)
        or not isinstance(argv, list)
        or len(argv) < 3
        or any(not isinstance(item, str) or not item for item in argv)
        or run_evidence.get("worker_argv") != argv
        or argv[1:3] != ["-m", "cfdpipe.coarse_repair_audit_worker"]
        or not _is_absolute_attested_path(argv[0])
        or worker.get("configured_process_memory_limit_bytes")
        != expected_worker_memory_limit_bytes
        or worker.get("hard_limit_installed_before_heavy_import") is not True
        or worker.get("heavy_dependencies_imported") is not True
        or worker.get("local_schedule_bound_after_heavy_import") is not True
        or worker.get("normalized_resource_matches_raw") is not True
        or worker.get("audit_runner_called") is not True
        or not _same_portable_attestation(worker.get("manifest_path"), str(path))
        or not _same_portable_attestation(
            worker.get("output_directory"), str(path.parent)
        )
        or not _is_absolute_attested_path(worker.get("evidence_path", ""))
        or isolation.get("coarse_contract_sha256")
        != str(expected_coarse_contract_sha256).casefold()
        or run_evidence.get("coarse_contract_sha256")
        != str(expected_coarse_contract_sha256).casefold()
        or run_evidence.get("schedule_feasibility_endpoint") != PATTERN_REQUEST
        or run_evidence.get("config_path") != worker.get("config_path")
        or run_evidence.get("config_sha256") != isolation.get("config_sha256")
        or hard_limit.get("schema") != "cfdpipe.worker_memory_limit.v1"
        or hard_limit.get("status") != "PASS"
        or hard_limit.get("supported") is not True
        or hard_limit.get("platform") != "win32"
        or hard_limit.get("backend") != "windows_job_object"
        or hard_limit.get("operation")
        != "install_current_process_memory_limit"
        or hard_limit.get("error") is not None
        or hard_limit.get("requested_process_memory_limit_bytes")
        != expected_worker_memory_limit_bytes
        or job_object.get("process_memory_limit_bytes")
        != expected_worker_memory_limit_bytes
        or job_object.get("process_assigned") is not True
        or job_object.get("handle_retained_for_process_lifetime") is not True
        or isinstance(job_object.get("job_handle_value"), bool)
        or not isinstance(job_object.get("job_handle_value"), int)
        or job_object["job_handle_value"] <= 0
        or isinstance(job_object.get("limit_flags"), bool)
        or not isinstance(job_object.get("limit_flags"), int)
        or job_object["limit_flags"] & 0x2100 != 0x2100
        or process_after_limit.get("schema")
        != "cfdpipe.worker_process_memory.v2"
        or process_after_limit.get("status") != "PASS"
        or process_after_limit.get("supported") is not True
        or process_after_audit.get("schema")
        != "cfdpipe.worker_process_memory.v2"
        or process_after_audit.get("status") != "PASS"
        or process_after_audit.get("supported") is not True
        or process_after_limit.get("process_id") != hard_limit.get("process_id")
        or process_after_audit.get("process_id") != hard_limit.get("process_id")
    ):
        raise CoarseDirectionReplayError(
            "direction audit worker isolation is not independently proven"
        )

    expected_projection_path = str(expected_projection_evidence.get("path", ""))
    expected_schedule_path = str(
        expected_local_schedule_binding.get("source_plan_path", "")
    )
    expected_schedule_sha = str(
        expected_local_schedule_binding.get("source_plan_sha256", "")
    ).casefold()
    try:
        argv_characteristic = _finite(
            _argv_option(argv, "--characteristic-length"),
            "direction audit argv characteristic length",
        )
    except (TypeError, ValueError) as error:
        raise CoarseDirectionReplayError(
            "direction audit worker argv is invalid"
        ) from error
    if (
        not expected_projection_path
        or not _is_absolute_attested_path(expected_projection_path)
        or not expected_schedule_path
        or not _is_absolute_attested_path(expected_schedule_path)
        or _SHA256.fullmatch(expected_schedule_sha) is None
        or _argv_option(argv, "--coarse-contract-sha256")
        != str(expected_coarse_contract_sha256).casefold()
        or not math.isclose(
            argv_characteristic,
            _finite(expected_characteristic_length_m, "expected characteristic length"),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
        or not _same_portable_attestation(
            _argv_option(argv, "--projection-manifest"),
            expected_projection_path,
        )
        or _argv_option(argv, "--projection-sha256")
        != str(expected_projection_evidence.get("sha256", "")).casefold()
        or not _same_portable_attestation(
            _argv_option(argv, "--local-schedule"), expected_schedule_path
        )
        or _argv_option(argv, "--local-schedule-sha256")
        != expected_schedule_sha
        or _argv_option(argv, "--schedule-feasibility-endpoint")
        != PATTERN_REQUEST
        or not _same_portable_attestation(
            _argv_option(argv, "--output"), str(path.parent)
        )
        or _argv_option(argv, "--config") != str(worker.get("config_path", ""))
        or _argv_option(argv, "--config-sha256")
        != str(isolation.get("config_sha256", ""))
    ):
        raise CoarseDirectionReplayError(
            "direction audit worker argv differs from its run evidence"
        )

    characteristic = _finite(
        manifest.get("characteristic_length_m"), "audit characteristic length"
    )
    strategy = manifest.get("strategy_config")
    if (
        manifest.get("coarse_contract_sha256")
        != str(expected_coarse_contract_sha256).casefold()
        or not math.isclose(
            characteristic,
            _finite(expected_characteristic_length_m, "expected characteristic length"),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
        or not _same_portable_attestation(strategy, expected_audit_strategy)
        or manifest.get("strategy_config_sha256")
        != strategy.get("normalized_config_sha256")
        or not _same_portable_attestation(
            manifest.get("local_schedule_binding"), expected_local_schedule_binding
        )
        or not _same_portable_attestation(
            manifest.get("projection_evidence"), expected_projection_evidence
        )
    ):
        raise CoarseDirectionReplayError("direction audit lineage differs from this run")

    discovery = manifest.get("repair_discovery")
    try:
        validated_discovery = validate_component_direction_discovery(
            discovery,
            expected_direction_endpoint=strategy[
                "owner_free_direction_endpoint"
            ],
        )
    except (CoarseComponentDirectionError, KeyError) as error:
        raise CoarseDirectionReplayError(
            f"direction audit discovery is invalid: {error}"
        ) from error
    if validated_discovery.get("status") != "PASS":
        raise CoarseDirectionReplayError("direction audit discovery is not PASS")

    try:
        process_id = int(
            isolation["worker_memory"]["process_after_audit"]["process_id"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CoarseDirectionReplayError(
            "direction audit worker process identity is missing"
        ) from error
    if process_id <= 0:
        raise CoarseDirectionReplayError(
            "direction audit worker process identity is invalid"
        )
    return {
        "source": {
            "path": str(path),
            "sha256": expected_sha,
            "size_bytes": path.stat().st_size,
            "schema": manifest["schema"],
            "status": manifest["status"],
            "worker_process_id": process_id,
            "started_at_utc": str(manifest.get("started_at_utc", "")),
            "ended_at_utc": str(manifest.get("ended_at_utc", "")),
        },
        "discovery": validated_discovery,
    }


def _consensus_payload(discovery: Mapping[str, Any]) -> dict[str, Any]:
    search = discovery["search"]
    return {
        "stable_observation": discovery["stable_observation"],
        "stable_observation_sha256": discovery["stable_observation_sha256"],
        "direction_endpoint": discovery["direction_endpoint"],
        "interaction_topology": search["interaction_topology"],
        "coordinate_replay": search["coordinate_replay"],
        "aba_replay": search["aba_replay"],
        "final_component_recheck": search["final_component_recheck"],
        "final_low_quality_prisms": search["final_low_quality_prisms"],
        "changed_roots": search["changed_roots"],
        "final_quality": discovery["final_quality"],
        "observed_counts": discovery["observed_counts"],
        "subdivision": discovery["subdivision"],
    }


def _root_contexts(
    stable_observation: Mapping[str, Any],
    interaction_topology: Mapping[str, Any],
    *,
    changed_root_ids: set[str],
) -> list[dict[str, Any]]:
    triangles = stable_observation["bad_source_triangles"]
    base_components = stable_observation["components"]
    interaction_components = interaction_topology["components"]
    candidate_ids = sorted(
        {
            str(root)
            for component in base_components
            for root in component["root_coordinate_sha256"]
        }
    )
    contexts: list[dict[str, Any]] = []
    for root_id in candidate_ids:
        incident = [
            record
            for record in triangles
            if root_id in record["root_coordinate_sha256"]
        ]
        base = [
            component
            for component in base_components
            if root_id in component["root_coordinate_sha256"]
        ]
        interaction = [
            component
            for component in interaction_components
            if root_id in component["root_coordinate_sha256"]
        ]
        if len(base) != 1 or len(interaction) != 1 or not incident:
            raise CoarseDirectionReplayError(
                "direction replay root context is not uniquely defined"
            )
        context: dict[str, Any] = {
            "root_coordinate_sha256": root_id,
            "incident_bad_source_triangle_sha256": sorted(
                str(record["source_triangle_sha256"]) for record in incident
            ),
            "incident_wall_surface_fingerprints": sorted(
                {
                    str(record["wall_surface_fingerprint"])
                    for record in incident
                }
            ),
            "base_component_sha256": str(base[0]["component_sha256"]),
            "interaction_component_sha256": str(
                interaction[0]["interaction_component_sha256"]
            ),
            "changed": root_id in changed_root_ids,
        }
        context["root_context_sha256"] = _canonical_sha256(context)
        contexts.append(context)
    return contexts


def load_direction_replay_consensus_approval(
    primary_path: str | os.PathLike[str],
    primary_sha256: str,
    corroborating_path: str | os.PathLike[str],
    corroborating_sha256: str,
    *,
    expected_audit_strategy: Mapping[str, Any],
    expected_coarse_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_local_schedule_binding: Mapping[str, Any],
    expected_projection_evidence: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_worker_memory_limit_bytes: int,
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Require two independent, byte-identified audits and derive consensus."""

    common = {
        "expected_audit_strategy": expected_audit_strategy,
        "expected_coarse_contract_sha256": expected_coarse_contract_sha256,
        "expected_characteristic_length_m": expected_characteristic_length_m,
        "expected_local_schedule_binding": expected_local_schedule_binding,
        "expected_projection_evidence": expected_projection_evidence,
        "expected_projected_3d_elements": expected_projected_3d_elements,
        "expected_worker_memory_limit_bytes": expected_worker_memory_limit_bytes,
        "expected_minimum_prism_scaled_jacobian": (
            expected_minimum_prism_scaled_jacobian
        ),
        "expected_minimum_core_tetra_gamma": expected_minimum_core_tetra_gamma,
    }
    attestations = [
        _load_direction_audit_attestation(
            primary_path, primary_sha256, **common
        ),
        _load_direction_audit_attestation(
            corroborating_path, corroborating_sha256, **common
        ),
    ]
    sources = sorted(
        [copy.deepcopy(record["source"]) for record in attestations],
        key=lambda record: record["sha256"],
    )
    if (
        len({record["path"] for record in sources}) != 2
        or len({record["sha256"] for record in sources}) != 2
        or len({record["worker_process_id"] for record in sources}) != 2
    ):
        raise CoarseDirectionReplayError(
            "direction replay attestations are not independent"
        )
    payloads = [
        _consensus_payload(record["discovery"]) for record in attestations
    ]
    if payloads[0] != payloads[1]:
        raise CoarseDirectionReplayError(
            "independent direction audits do not have identical consensus evidence"
        )
    payload = payloads[0]
    discovery = attestations[0]["discovery"]
    search = discovery["search"]
    replay = search["coordinate_replay"]
    raw_changed = search["changed_roots"]
    changed_roots: list[dict[str, Any]] = []
    for record in raw_changed:
        changed_roots.append(
            {
                "root_coordinate_sha256": str(record["root_coordinate_sha256"]),
                "root_context_sha256": "PENDING",
                "original_direction_float_hex": _float_hex_vector(
                    record["original_direction"], "original direction"
                ),
                "selected_direction_float_hex": _float_hex_vector(
                    record["selected_direction"], "selected direction"
                ),
                "selected_source": str(record["selected_source"]),
                "selected_interior_weight_float_hex": _finite(
                    record["selected_interior_weight"], "selected interior weight"
                ).hex(),
            }
        )
    changed_roots.sort(key=lambda record: record["root_coordinate_sha256"])
    changed_ids = {
        str(record["root_coordinate_sha256"]) for record in changed_roots
    }
    contexts = _root_contexts(
        discovery["stable_observation"],
        search["interaction_topology"],
        changed_root_ids=changed_ids,
    )
    context_by_id = {
        str(record["root_coordinate_sha256"]): record for record in contexts
    }
    for record in changed_roots:
        record["root_context_sha256"] = context_by_id[
            record["root_coordinate_sha256"]
        ]["root_context_sha256"]

    consensus: dict[str, Any] = {
        "schema": "cfdpipe.coarse_direction_replay_consensus.v1",
        "status": "PASS",
        "attestation_count": 2,
        "stable_observation_sha256": str(
            discovery["stable_observation_sha256"]
        ),
        "schedule_endpoint_sha256": str(
            discovery["subdivision"]["schedule_endpoint_sha256"]
        ),
        "direction_endpoint_sha256": str(
            discovery["direction_endpoint"]["endpoint_sha256"]
        ),
        "interaction_topology_sha256": str(
            search["interaction_topology"]["interaction_topology_sha256"]
        ),
        "state_a_coordinate_sha256": str(
            replay["state_a_first_coordinate_sha256"]
        ),
        "state_b_coordinate_sha256": str(
            replay["state_b_first_coordinate_sha256"]
        ),
        "state_a_quality_sha256": str(
            replay["state_a_first_quality_sha256"]
        ),
        "state_b_quality_sha256": str(
            replay["state_b_first_quality_sha256"]
        ),
        "final_quality_sha256": str(
            discovery["final_quality"]["quality_sha256"]
        ),
        "final_component_recheck_sha256": _canonical_sha256(
            search["final_component_recheck"]
        ),
        "final_low_quality_prisms_sha256": _canonical_sha256(
            search["final_low_quality_prisms"]
        ),
        "changed_roots_sha256": _canonical_sha256(changed_roots),
    }
    consensus["consensus_sha256"] = _canonical_sha256(consensus)
    approval: dict[str, Any] = {
        "schema": APPROVAL_SCHEMA,
        "status": "PASS",
        "source_audit_manifests": sources,
        "coarse_contract_sha256": str(expected_coarse_contract_sha256).casefold(),
        "characteristic_length_m": float(expected_characteristic_length_m),
        "local_schedule_binding_sha256": str(
            expected_local_schedule_binding.get("binding_sha256", "")
        ).casefold(),
        "projection_manifest_sha256": str(
            expected_projection_evidence.get("sha256", "")
        ).casefold(),
        "schedule_feasibility_endpoint": copy.deepcopy(
            expected_audit_strategy["schedule_feasibility_endpoint"]
        ),
        "direction_endpoint": copy.deepcopy(
            expected_audit_strategy["owner_free_direction_endpoint"]
        ),
        "consensus": consensus,
        "source_minimum_growth_final_quality": copy.deepcopy(
            discovery["final_quality"]
        ),
        "candidate_root_count": int(search["candidate_root_count"]),
        "changed_root_count": len(changed_roots),
        "root_contexts": contexts,
        "changed_roots": changed_roots,
        "runtime_mesh_tags_hardcoded": False,
        "audit_only_sources_preserved": True,
        "audit_only_direction_replay_authorized": True,
        "mesh_write_authorized": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
    }
    approval["approval_sha256"] = _canonical_sha256(approval)
    return validate_direction_replay_approval(
        approval,
        expected_coarse_contract_sha256=expected_coarse_contract_sha256,
        expected_characteristic_length_m=expected_characteristic_length_m,
        expected_local_schedule_binding_sha256=str(
            expected_local_schedule_binding.get("binding_sha256", "")
        ),
        expected_projection_manifest_sha256=str(
            expected_projection_evidence.get("sha256", "")
        ),
        expected_projected_3d_elements=expected_projected_3d_elements,
        expected_minimum_prism_scaled_jacobian=(
            expected_minimum_prism_scaled_jacobian
        ),
        expected_minimum_core_tetra_gamma=expected_minimum_core_tetra_gamma,
    )


__all__ = [
    "APPROVAL_SCHEMA",
    "DEFAULT_PHYSICAL_SCHEDULE_HOMOTOPY_FRACTIONS",
    "HOMOTOPY_APPLICATION_SCHEMA",
    "HOMOTOPY_AUDIT_VARIANT",
    "HOMOTOPY_DISCOVERY_SCHEMA",
    "HOMOTOPY_ENDPOINT_SCHEMA",
    "HOMOTOPY_INCOMPLETE_REASON",
    "HOMOTOPY_MODE",
    "PHYSICAL_DISCOVERY_SCHEMA",
    "PHYSICAL_HOMOTOPY_DISCOVERY_SCHEMA",
    "CoarseDirectionReplayError",
    "interpolate_physical_schedule_homotopy",
    "load_direction_replay_consensus_approval",
    "make_physical_schedule_homotopy_endpoint",
    "validate_direction_replay_approval",
    "validate_physical_schedule_homotopy_discovery",
    "validate_physical_schedule_homotopy_endpoint",
    "validate_physical_schedule_replay_discovery",
]
