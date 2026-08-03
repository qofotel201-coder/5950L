"""Evidence-oriented boundary-layer stack planning for production mesh gates.

The existing smoke mesh proves the first-cell height with a real SST solution,
but its thin prism stack is not an outer-boundary-layer model.  This module
adds a separate planning gate: it estimates a conservative delta99 scale from
the configured design condition and rejects a prism stack that cannot cover it
or transition safely to the requested core size.  The estimate is deliberately
labelled as planning evidence; a solution-based y+ and layer-coverage audit is
still mandatory.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .atmosphere import AtmosphereError, us_standard_atmosphere_1976


_METHOD = "turbulent_flat_plate_delta99_0p37_re_x_minus_one_fifth"


class BoundaryLayerDesignError(RuntimeError):
    """Raised when the proposed production boundary-layer stack fails closed."""


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerDesignError(f"{label} must be finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerDesignError(
            f"{label} must be finite numeric data"
        ) from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        raise BoundaryLayerDesignError(f"{label} must be finite and positive")
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerDesignError(f"{label} must be a positive integer")
    return value


def design_boundary_layer_stack(
    *,
    project: Mapping[str, Any],
    selected_case: Mapping[str, Any],
    first_layer_height_m: float,
    estimated_yplus: float,
    core_size_m: float,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a fail-closed production prism-stack planning record.

    All project quantities are supplied by the caller from configuration.  The
    only supported correlation is named explicitly in ``policy`` so a future
    method change cannot occur silently.
    """

    expected_policy = {
        "method",
        "thickness_safety_factor",
        "maximum_layer_count",
        "maximum_core_to_last_layer_ratio",
    }
    if set(policy) != expected_policy or policy.get("method") != _METHOD:
        raise BoundaryLayerDesignError("unsupported boundary-layer thickness method")

    reference = project.get("reference")
    mesh = project.get("mesh")
    atmosphere = project.get("atmosphere")
    if not all(isinstance(value, Mapping) for value in (reference, mesh, atmosphere)):
        raise BoundaryLayerDesignError("project boundary-layer inputs are incomplete")

    case_id = str(selected_case.get("case_id", "")).strip()
    mach = _finite(selected_case.get("mach"), "case Mach", positive=True)
    altitude_km = _finite(selected_case.get("altitude_km"), "case altitude")
    reference_length = _finite(
        reference.get("length_m"), "reference length", positive=True
    )
    minimum_layers = _positive_integer(
        mesh.get("minimum_prism_layers"), "minimum prism layers"
    )
    target_layers = _positive_integer(
        mesh.get("target_prism_layers"), "target prism layers"
    )
    maximum_layers = _positive_integer(
        policy.get("maximum_layer_count"), "maximum layer count"
    )
    growth = _finite(mesh.get("initial_growth_ratio"), "growth ratio", positive=True)
    safety = _finite(
        policy.get("thickness_safety_factor"),
        "thickness safety factor",
        positive=True,
    )
    maximum_transition = _finite(
        policy.get("maximum_core_to_last_layer_ratio"),
        "maximum core-to-last-layer ratio",
        positive=True,
    )
    first_height = _finite(
        first_layer_height_m, "first layer height", positive=True
    )
    planned_yplus = _finite(estimated_yplus, "estimated y+", positive=True)
    core_size = _finite(core_size_m, "core size", positive=True)
    if (
        not case_id
        or altitude_km < 0.0
        or minimum_layers < 15
        or target_layers < minimum_layers
        or maximum_layers < target_layers
        or not 1.0 < growth <= 1.5
        or safety < 1.0
        or planned_yplus > 1.0
    ):
        raise BoundaryLayerDesignError("configured boundary-layer policy is unsafe")

    try:
        state = us_standard_atmosphere_1976(
            altitude_km * 1000.0,
            model=str(atmosphere.get("model", "")),
            altitude_kind=str(atmosphere.get("altitude_kind", "")),
            viscosity_model=str(atmosphere.get("viscosity_model", "")),
        )
    except AtmosphereError as error:
        raise BoundaryLayerDesignError(
            f"configured atmosphere is invalid: {error}"
        ) from error

    velocity = mach * state.speed_of_sound_m_s
    reynolds = (
        state.density_kg_m3
        * velocity
        * reference_length
        / state.dynamic_viscosity_pa_s
    )
    delta99 = 0.37 * reference_length / reynolds ** (1.0 / 5.0)
    required_thickness = delta99 * safety
    if any(
        not math.isfinite(value) or value <= 0.0
        for value in (velocity, reynolds, delta99, required_thickness)
    ):
        raise BoundaryLayerDesignError("boundary-layer estimate is non-finite")

    layer_count = max(minimum_layers, target_layers)
    total_thickness = first_height * (growth**layer_count - 1.0) / (growth - 1.0)
    while total_thickness < required_thickness and layer_count < maximum_layers:
        layer_count += 1
        total_thickness = (
            first_height * (growth**layer_count - 1.0) / (growth - 1.0)
        )
    if total_thickness < required_thickness:
        raise BoundaryLayerDesignError(
            "boundary-layer layer cap cannot cover the required outer thickness"
        )

    last_layer = first_height * growth ** (layer_count - 1)
    transition_ratio = core_size / last_layer
    if not math.isfinite(transition_ratio) or transition_ratio > maximum_transition:
        raise BoundaryLayerDesignError(
            "core-to-last-layer transition exceeds the configured ratio"
        )

    cumulative: list[float] = []
    running = 0.0
    thicknesses: list[float] = []
    for index in range(layer_count):
        thickness = first_height * growth**index
        thicknesses.append(thickness)
        running += thickness
        cumulative.append(running)
    finite_audit_values = [
        velocity,
        reynolds,
        delta99,
        required_thickness,
        first_height,
        last_layer,
        total_thickness,
        transition_ratio,
        *thicknesses,
        *cumulative,
    ]
    if any(not math.isfinite(value) or value <= 0.0 for value in finite_audit_values):
        raise BoundaryLayerDesignError("final boundary-layer design is non-finite")

    return {
        "schema": "cfdpipe.boundary_layer_design.v1",
        "status": "PASS",
        "case_id": case_id,
        "method": _METHOD,
        "planning_estimate_only": True,
        "requires_solution_based_validation": True,
        "atmosphere": state.as_dict(),
        "mach": mach,
        "altitude_km": altitude_km,
        "freestream_velocity_m_s": velocity,
        "reference_length_m": reference_length,
        "reference_reynolds_number": reynolds,
        "estimated_delta99_m": delta99,
        "thickness_safety_factor": safety,
        "required_total_thickness_m": required_thickness,
        "first_layer_height_m": first_height,
        "estimated_first_layer_yplus": planned_yplus,
        "project_minimum_layer_count": minimum_layers,
        "project_target_layer_count": target_layers,
        "maximum_layer_count": maximum_layers,
        "layer_count": layer_count,
        "growth_ratio": growth,
        "layer_thicknesses_m": thicknesses,
        "cumulative_heights_m": cumulative,
        "last_layer_height_m": last_layer,
        "total_thickness_m": total_thickness,
        "core_size_m": core_size,
        "core_to_last_layer_ratio": transition_ratio,
        "maximum_core_to_last_layer_ratio": maximum_transition,
        "finite_audit_values": finite_audit_values,
    }


__all__ = ["BoundaryLayerDesignError", "design_boundary_layer_stack"]
