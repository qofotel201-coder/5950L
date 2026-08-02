"""Strict audit-only boundary-layer schedule feasibility endpoints.

The production schedule remains owned by the hash-bound local schedule plan.
This module can derive one deliberately conservative in-memory candidate from
that plan: keep the evidenced first-cell height and layer count, but set every
layer increment to the first-cell height (growth ratio 1).  The candidate is
only an endpoint probe.  It cannot authorize calibration, mesh output, SU2 or
ParaView.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


ENDPOINT_SCHEMA = "cfdpipe.coarse_schedule_feasibility_endpoint.v1"
APPLICATION_SCHEMA = "cfdpipe.coarse_schedule_feasibility_application.v1"
ENDPOINT_MODE = "uniform_minimum_growth"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseScheduleFeasibilityError(RuntimeError):
    """A schedule endpoint or its application is unsafe or stale."""


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoarseScheduleFeasibilityError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise CoarseScheduleFeasibilityError(
            f"{label} must be finite and positive"
        )
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseScheduleFeasibilityError(
            f"{label} must be a positive integer"
        )
    return value


def make_minimum_growth_endpoint(
    *,
    baseline_binding_sha256: str,
    first_layer_height_m: float,
    layer_count: int,
) -> dict[str, Any]:
    """Create the one permitted audit-only minimum-growth endpoint."""

    binding_sha256 = str(baseline_binding_sha256).casefold()
    if _SHA256.fullmatch(binding_sha256) is None:
        raise CoarseScheduleFeasibilityError(
            "baseline schedule binding SHA-256 is invalid"
        )
    first = _finite_positive(first_layer_height_m, "first layer height")
    layers = _positive_int(layer_count, "layer count")
    endpoint: dict[str, Any] = {
        "schema": ENDPOINT_SCHEMA,
        "status": "AUDIT_ONLY",
        "mode": ENDPOINT_MODE,
        "baseline_binding_sha256": binding_sha256,
        "first_layer_height_m": first,
        "layer_count": layers,
        "candidate_growth_ratio": 1.0,
        "preserve_first_layer_height": True,
        "preserve_layer_count": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    endpoint["endpoint_sha256"] = _canonical_sha256(endpoint)
    return endpoint


def validate_minimum_growth_endpoint(
    value: Any,
    *,
    expected_binding_sha256: str,
    expected_first_layer_height_m: float,
    expected_layer_count: int,
) -> dict[str, Any]:
    """Validate the exact endpoint contract and return a defensive copy."""

    if not isinstance(value, Mapping):
        raise CoarseScheduleFeasibilityError(
            "schedule feasibility endpoint must be a mapping"
        )
    expected_fields = {
        "schema",
        "status",
        "mode",
        "baseline_binding_sha256",
        "first_layer_height_m",
        "layer_count",
        "candidate_growth_ratio",
        "preserve_first_layer_height",
        "preserve_layer_count",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "mesh_written",
        "su2_called",
        "paraview_called",
        "endpoint_sha256",
    }
    if set(value) != expected_fields:
        raise CoarseScheduleFeasibilityError(
            "schedule feasibility endpoint schema is incomplete"
        )
    unsigned = dict(value)
    configured_hash = str(unsigned.pop("endpoint_sha256", "")).casefold()
    expected_binding = str(expected_binding_sha256).casefold()
    raw_growth = value.get("candidate_growth_ratio")
    expected_first = _finite_positive(
        expected_first_layer_height_m, "expected first layer height"
    )
    expected_layers = _positive_int(expected_layer_count, "expected layer count")
    candidate_first = _finite_positive(
        value.get("first_layer_height_m"), "candidate first layer height"
    )
    candidate_layers = _positive_int(value.get("layer_count"), "candidate layer count")
    if (
        value.get("schema") != ENDPOINT_SCHEMA
        or value.get("status") != "AUDIT_ONLY"
        or value.get("mode") != ENDPOINT_MODE
        or _SHA256.fullmatch(expected_binding) is None
        or value.get("baseline_binding_sha256") != expected_binding
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
        or not math.isclose(
            candidate_first, expected_first, rel_tol=1.0e-12, abs_tol=0.0
        )
        or candidate_layers != expected_layers
        or isinstance(raw_growth, bool)
        or not isinstance(raw_growth, (int, float))
        or not math.isfinite(float(raw_growth))
        or float(raw_growth) != 1.0
        or value.get("preserve_first_layer_height") is not True
        or value.get("preserve_layer_count") is not True
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_written") is not False
        or value.get("su2_called") is not False
        or value.get("paraview_called") is not False
    ):
        raise CoarseScheduleFeasibilityError(
            "schedule feasibility endpoint is stale or unsafe"
        )
    return copy.deepcopy(dict(value))


def apply_minimum_growth_endpoint(
    surface_schedules: Mapping[str, Sequence[float]],
    endpoint: Mapping[str, Any],
    *,
    expected_surface_fingerprints: Sequence[str],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Apply the endpoint without mutating the bound baseline schedules."""

    fingerprints = [str(value).casefold() for value in expected_surface_fingerprints]
    if (
        not fingerprints
        or len(fingerprints) != len(set(fingerprints))
        or any(_SHA256.fullmatch(value) is None for value in fingerprints)
    ):
        raise CoarseScheduleFeasibilityError(
            "expected wall fingerprint inventory is invalid"
        )
    if not isinstance(surface_schedules, Mapping):
        raise CoarseScheduleFeasibilityError("baseline surface schedules are missing")
    normalized_keys = [str(value).casefold() for value in surface_schedules]
    if (
        len(normalized_keys) != len(set(normalized_keys))
        or set(normalized_keys) != set(fingerprints)
    ):
        raise CoarseScheduleFeasibilityError(
            "baseline surface schedules do not match the stable wall inventory"
        )

    expected_first = _finite_positive(
        endpoint.get("first_layer_height_m"), "endpoint first layer height"
    )
    expected_layers = _positive_int(endpoint.get("layer_count"), "endpoint layer count")
    validated_endpoint = validate_minimum_growth_endpoint(
        endpoint,
        expected_binding_sha256=str(endpoint.get("baseline_binding_sha256", "")),
        expected_first_layer_height_m=expected_first,
        expected_layer_count=expected_layers,
    )

    baseline: dict[str, list[float]] = {}
    for raw_fingerprint, raw_values in surface_schedules.items():
        fingerprint = str(raw_fingerprint).casefold()
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes, bytearray)
        ):
            raise CoarseScheduleFeasibilityError(
                "one baseline cumulative schedule is not a numeric sequence"
            )
        values = [
            _finite_positive(value, "baseline cumulative height")
            for value in raw_values
        ]
        if (
            len(values) != expected_layers
            or any(right <= left for left, right in zip(values, values[1:]))
            or not math.isclose(
                values[0], expected_first, rel_tol=1.0e-12, abs_tol=0.0
            )
        ):
            raise CoarseScheduleFeasibilityError(
                "one baseline schedule violates the fixed endpoint invariants"
            )
        baseline[fingerprint] = values

    candidate_values = [expected_first * float(index) for index in range(1, expected_layers + 1)]
    if (
        candidate_values[0] != expected_first
        or len(candidate_values) != expected_layers
        or any(right <= left for left, right in zip(candidate_values, candidate_values[1:]))
        or any(not math.isfinite(value) for value in candidate_values)
    ):
        raise CoarseScheduleFeasibilityError(
            "minimum-growth endpoint construction is invalid"
        )
    candidate = {
        fingerprint: list(candidate_values) for fingerprint in sorted(fingerprints)
    }
    baseline_sorted = {key: baseline[key] for key in sorted(baseline)}
    evidence: dict[str, Any] = {
        "schema": APPLICATION_SCHEMA,
        "status": "PASS",
        "audit_only": True,
        "endpoint_sha256": validated_endpoint["endpoint_sha256"],
        "baseline_binding_sha256": validated_endpoint[
            "baseline_binding_sha256"
        ],
        "baseline_surface_schedules_sha256": _canonical_sha256(baseline_sorted),
        "candidate_surface_schedules_sha256": _canonical_sha256(candidate),
        "surface_count": len(candidate),
        "layer_count": expected_layers,
        "first_layer_height_m": expected_first,
        "candidate_growth_ratio": 1.0,
        "candidate_total_thickness_m": candidate_values[-1],
        "first_layer_height_preserved": all(
            values[0] == expected_first for values in candidate.values()
        ),
        "layer_count_preserved": all(
            len(values) == expected_layers for values in candidate.values()
        ),
        "baseline_input_mutated": False,
        "runtime_entity_tags_present": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    evidence["application_sha256"] = _canonical_sha256(evidence)
    return candidate, evidence


def validate_minimum_growth_application(
    value: Any,
    *,
    endpoint: Mapping[str, Any],
    baseline_surface_schedules: Mapping[str, Sequence[float]],
    expected_surface_fingerprints: Sequence[str],
) -> dict[str, Any]:
    """Prove that the recorded candidate is the exact endpoint application."""

    if not isinstance(value, Mapping):
        raise CoarseScheduleFeasibilityError(
            "schedule feasibility application must be a mapping"
        )
    _, expected = apply_minimum_growth_endpoint(
        baseline_surface_schedules,
        endpoint,
        expected_surface_fingerprints=expected_surface_fingerprints,
    )
    if dict(value) != expected:
        raise CoarseScheduleFeasibilityError(
            "schedule feasibility application evidence is stale or incomplete"
        )
    return copy.deepcopy(dict(value))


__all__ = [
    "APPLICATION_SCHEMA",
    "CoarseScheduleFeasibilityError",
    "ENDPOINT_MODE",
    "ENDPOINT_SCHEMA",
    "apply_minimum_growth_endpoint",
    "make_minimum_growth_endpoint",
    "validate_minimum_growth_application",
    "validate_minimum_growth_endpoint",
]
