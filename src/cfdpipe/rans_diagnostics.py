"""Pure-Python diagnostics for the bounded SST RANS smoke gate.

This module deliberately has no ParaView import.  A ``pvbatch`` entry point
fetches the VTK data and passes ordinary coordinates/arrays into these
testable routines.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .outlet_diagnostics import (
    SU2TopologyMesh,
    Vector,
    map_mesh_points_to_solution,
    split_linear_triangle,
)


class RANSDiagnosticError(RuntimeError):
    """Raised when actual RANS evidence is incomplete or invalid."""


def _finite(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise RANSDiagnosticError(f"{label} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise RANSDiagnosticError(f"{label} must be finite") from error
    if not math.isfinite(result):
        raise RANSDiagnosticError(f"{label} must be finite")
    return result


def _sub(left: Vector, right: Vector) -> Vector:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _cross(left: Vector, right: Vector) -> Vector:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: Vector) -> float:
    return math.sqrt(sum(component * component for component in value))


def _triangle_area(points: Sequence[Vector]) -> float:
    return 0.5 * _norm(_cross(_sub(points[1], points[0]), _sub(points[2], points[0])))


def _validate_vector(values: Sequence[object], *, label: str) -> tuple[float, float, float]:
    if isinstance(values, (str, bytes)) or len(values) != 3:
        raise RANSDiagnosticError(f"{label} must contain three finite values")
    return tuple(
        _finite(value, label=f"{label}[{index}]")
        for index, value in enumerate(values)
    )  # type: ignore[return-value]


def _clip_triangle_fields(
    vertices: Sequence[tuple[Vector, float, tuple[float, ...]]],
    *,
    keep_positive: bool,
) -> list[tuple[Vector, float, tuple[float, ...]]]:
    result: list[tuple[Vector, float, tuple[float, ...]]] = []
    for current, following in zip(vertices, (*vertices[1:], vertices[0]), strict=True):
        current_inside = current[1] >= 0.0 if keep_positive else current[1] <= 0.0
        following_inside = following[1] >= 0.0 if keep_positive else following[1] <= 0.0
        if current_inside:
            result.append(current)
        if current_inside == following_inside:
            continue
        denominator = following[1] - current[1]
        if denominator == 0.0:
            continue
        fraction = -current[1] / denominator
        point = tuple(
            current[0][axis] + fraction * (following[0][axis] - current[0][axis])
            for axis in range(3)
        )
        fields = tuple(
            current[2][index] + fraction * (following[2][index] - current[2][index])
            for index in range(len(current[2]))
        )
        result.append((point, 0.0, fields))  # type: ignore[arg-type]
    cleaned: list[tuple[Vector, float, tuple[float, ...]]] = []
    for vertex in result:
        if not cleaned or vertex[0] != cleaned[-1][0]:
            cleaned.append(vertex)
    if len(cleaned) > 1 and cleaned[0][0] == cleaned[-1][0]:
        cleaned.pop()
    if len({vertex[0] for vertex in cleaned}) < 3:
        return []
    return cleaned


def _integrate_flux_polygon(
    polygon: Sequence[tuple[Vector, float, tuple[float, ...]]],
) -> tuple[float, float, list[float]]:
    if len(polygon) < 3:
        return 0.0, 0.0, []
    field_count = len(polygon[0][2])
    area_total = 0.0
    flux_total = 0.0
    weighted = [0.0] * field_count
    for index in range(1, len(polygon) - 1):
        triangle = (polygon[0], polygon[index], polygon[index + 1])
        area = _triangle_area(tuple(vertex[0] for vertex in triangle))
        if not math.isfinite(area):
            raise RANSDiagnosticError("measurement clipping produced non-finite area")
        if area <= 0.0:
            continue
        fluxes = tuple(vertex[1] for vertex in triangle)
        area_total += area
        flux_total += area * sum(fluxes) / 3.0
        for field_index in range(field_count):
            field_values = tuple(vertex[2][field_index] for vertex in triangle)
            # Exact integral of the product of two linear finite-element fields.
            weighted[field_index] += area * (
                sum(fluxes) * sum(field_values)
                + sum(
                    fluxes[vertex_index] * field_values[vertex_index]
                    for vertex_index in range(3)
                )
            ) / 12.0
    return area_total, flux_total, weighted


def _cell_components(cells: Sequence[tuple[int, ...]]) -> list[list[int]]:
    point_to_cells: dict[int, list[int]] = {}
    for cell_index, cell in enumerate(cells):
        for point_id in cell:
            point_to_cells.setdefault(point_id, []).append(cell_index)
    remaining = set(range(len(cells)))
    components: list[list[int]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        stack = [start]
        component: list[int] = []
        while stack:
            current = stack.pop()
            component.append(current)
            neighbors = {
                neighbor
                for point_id in cells[current]
                for neighbor in point_to_cells[point_id]
            }
            new_neighbors = sorted(neighbors & remaining, reverse=True)
            remaining.difference_update(new_neighbors)
            stack.extend(new_neighbors)
        components.append(sorted(component))
    return components


def summarize_measurement_slice(
    *,
    points: Sequence[Vector],
    cells: Sequence[Sequence[int]],
    density: Sequence[float],
    momentum: Sequence[Vector],
    mach: Sequence[float],
    pressure: Sequence[float],
    temperature: Sequence[float],
    name: str,
    plane_coordinate_m: float,
    normal_unit: Sequence[float],
    bounds_m: Sequence[float],
    expected_area_m2: float,
    maximum_relative_area_error: float,
    minimum_selected_cell_count: int,
    specific_heat_ratio: float,
    freestream_static_pressure_pa: float,
    freestream_mach: float,
) -> dict[str, Any]:
    """Select one confirmed slice component and integrate conservative mass flux."""

    if not isinstance(name, str) or not name.strip():
        raise RANSDiagnosticError("measurement name is missing")
    coordinate = _finite(plane_coordinate_m, label="measurement plane coordinate")
    normal = _validate_vector(normal_unit, label="measurement normal")
    normal_norm = _norm(normal)
    if abs(normal_norm - 1.0) > 1.0e-12:
        raise RANSDiagnosticError("measurement normal must be a unit vector")
    if isinstance(bounds_m, (str, bytes)) or len(bounds_m) != 6:
        raise RANSDiagnosticError("measurement bounds must contain six finite values")
    bounds = tuple(
        _finite(value, label=f"measurement bounds[{index}]")
        for index, value in enumerate(bounds_m)
    )
    if any(bounds[index] > bounds[index + 3] for index in range(3)):
        raise RANSDiagnosticError("measurement bounds are inverted")
    expected_area = _finite(expected_area_m2, label="confirmed measurement area")
    relative_tolerance = _finite(
        maximum_relative_area_error, label="measurement area tolerance"
    )
    if expected_area <= 0.0 or not 0.0 < relative_tolerance <= 0.5:
        raise RANSDiagnosticError("invalid confirmed measurement area contract")
    if (
        isinstance(minimum_selected_cell_count, bool)
        or not isinstance(minimum_selected_cell_count, int)
        or minimum_selected_cell_count <= 0
    ):
        raise RANSDiagnosticError("minimum selected cell count must be positive")
    gamma = _finite(specific_heat_ratio, label="specific heat ratio")
    freestream_pressure = _finite(
        freestream_static_pressure_pa, label="freestream static pressure"
    )
    freestream_mach_value = _finite(freestream_mach, label="freestream Mach")
    if gamma <= 1.0 or freestream_pressure <= 0.0 or freestream_mach_value < 0.0:
        raise RANSDiagnosticError("invalid gas/freestream measurement contract")

    point_count = len(points)
    arrays = (density, momentum, mach, pressure, temperature)
    if point_count == 0 or any(len(array) != point_count for array in arrays):
        raise RANSDiagnosticError("measurement point arrays have inconsistent lengths")
    normalized_points = tuple(
        _validate_vector(point, label=f"measurement point {index}")
        for index, point in enumerate(points)
    )
    normalized_density: list[float] = []
    normalized_momentum: list[Vector] = []
    normalized_mach: list[float] = []
    normalized_pressure: list[float] = []
    normalized_temperature: list[float] = []
    total_pressure: list[float] = []
    exponent = gamma / (gamma - 1.0)
    for index in range(point_count):
        rho = _finite(density[index], label=f"measurement density {index}")
        momentum_value = _validate_vector(
            momentum[index], label=f"measurement momentum {index}"
        )
        mach_value = _finite(mach[index], label=f"measurement Mach {index}")
        pressure_value = _finite(
            pressure[index], label=f"measurement pressure {index}"
        )
        temperature_value = _finite(
            temperature[index], label=f"measurement temperature {index}"
        )
        if rho <= 0.0 or mach_value < 0.0 or pressure_value <= 0.0 or temperature_value <= 0.0:
            raise RANSDiagnosticError(f"measurement has a non-physical value at point {index}")
        stagnation_pressure = pressure_value * (
            1.0 + 0.5 * (gamma - 1.0) * mach_value * mach_value
        ) ** exponent
        if not math.isfinite(stagnation_pressure) or stagnation_pressure <= 0.0:
            raise RANSDiagnosticError(
                f"measurement total pressure is invalid at point {index}"
            )
        normalized_density.append(rho)
        normalized_momentum.append(momentum_value)
        normalized_mach.append(mach_value)
        normalized_pressure.append(pressure_value)
        normalized_temperature.append(temperature_value)
        total_pressure.append(stagnation_pressure)

    diagonal = math.sqrt(
        sum((bounds[index + 3] - bounds[index]) ** 2 for index in range(3))
    )
    geometric_tolerance = max(1.0e-8, 1.0e-5 * max(diagonal, 1.0e-3))
    if not bounds[0] - geometric_tolerance <= coordinate <= bounds[3] + geometric_tolerance:
        raise RANSDiagnosticError("measurement plane is outside confirmed bounds")

    bounded_cells: list[tuple[int, ...]] = []
    for cell_index, raw_cell in enumerate(cells):
        if isinstance(raw_cell, (str, bytes)) or len(raw_cell) < 3:
            raise RANSDiagnosticError(f"measurement cell {cell_index} is invalid")
        try:
            cell = tuple(int(point_id) for point_id in raw_cell)
        except (TypeError, ValueError) as error:
            raise RANSDiagnosticError(
                f"measurement cell {cell_index} has an invalid point id"
            ) from error
        if len(set(cell)) != len(cell) or any(
            point_id < 0 or point_id >= point_count for point_id in cell
        ):
            raise RANSDiagnosticError(f"measurement cell {cell_index} is degenerate")
        if all(
            all(
                bounds[axis] - geometric_tolerance
                <= normalized_points[point_id][axis]
                <= bounds[axis + 3] + geometric_tolerance
                for axis in range(3)
            )
            for point_id in cell
        ):
            bounded_cells.append(cell)
    if not bounded_cells:
        raise RANSDiagnosticError("measurement bounds selected no slice cells")

    components = _cell_components(bounded_cells)
    candidates: list[tuple[list[int], float, float, tuple[float, ...]]] = []
    for component in components:
        component_area = 0.0
        point_ids: set[int] = set()
        for bounded_index in component:
            cell = bounded_cells[bounded_index]
            point_ids.update(cell)
            for triangle_index in range(1, len(cell) - 1):
                triangle_points = (
                    normalized_points[cell[0]],
                    normalized_points[cell[triangle_index]],
                    normalized_points[cell[triangle_index + 1]],
                )
                area = _triangle_area(triangle_points)
                if not math.isfinite(area) or area <= 0.0:
                    raise RANSDiagnosticError("measurement contains a degenerate slice cell")
                component_area += area
        relative_error = abs(component_area - expected_area) / expected_area
        component_bounds = tuple(
            min(normalized_points[point_id][axis] for point_id in point_ids)
            for axis in range(3)
        ) + tuple(
            max(normalized_points[point_id][axis] for point_id in point_ids)
            for axis in range(3)
        )
        if len(component) >= minimum_selected_cell_count and relative_error <= relative_tolerance:
            candidates.append((component, component_area, relative_error, component_bounds))
    if len(candidates) != 1:
        closest = min(
            (
                abs(
                    sum(
                        _triangle_area(
                            (
                                normalized_points[bounded_cells[index][0]],
                                normalized_points[bounded_cells[index][triangle]],
                                normalized_points[bounded_cells[index][triangle + 1]],
                            )
                        )
                        for index in component
                        for triangle in range(1, len(bounded_cells[index]) - 1)
                    )
                    - expected_area
                )
                / expected_area
                for component in components
            ),
            default=math.inf,
        )
        raise RANSDiagnosticError(
            "confirmed measurement component did not match uniquely; "
            f"components={len(components)}, matches={len(candidates)}, "
            f"closest_relative_area_error={closest:.6g}"
        )

    component, component_area, relative_area_error, component_bounds = candidates[0]
    net_mass = 0.0
    forward_mass = 0.0
    reverse_mass = 0.0
    weighted = [0.0, 0.0, 0.0, 0.0]
    for bounded_index in component:
        cell = bounded_cells[bounded_index]
        for triangle_index in range(1, len(cell) - 1):
            point_ids = (cell[0], cell[triangle_index], cell[triangle_index + 1])
            vertices = tuple(
                (
                    normalized_points[point_id],
                    sum(
                        normalized_momentum[point_id][axis] * normal[axis]
                        for axis in range(3)
                    ),
                    (
                        normalized_mach[point_id],
                        normalized_pressure[point_id],
                        normalized_temperature[point_id],
                        total_pressure[point_id],
                    ),
                )
                for point_id in point_ids
            )
            _, triangle_net, _ = _integrate_flux_polygon(vertices)
            positive = _clip_triangle_fields(vertices, keep_positive=True)
            negative = _clip_triangle_fields(vertices, keep_positive=False)
            _, positive_flux, positive_weighted = _integrate_flux_polygon(positive)
            _, negative_flux, _ = _integrate_flux_polygon(negative)
            forward = max(0.0, positive_flux)
            reverse = max(0.0, -negative_flux)
            closure_scale = max(abs(triangle_net), forward + reverse, 1.0)
            if abs((forward - reverse) - triangle_net) > 1.0e-10 * closure_scale:
                raise RANSDiagnosticError("measurement positive/reverse mass split does not close")
            net_mass += triangle_net
            forward_mass += forward
            reverse_mass += reverse
            for field_index, value in enumerate(positive_weighted):
                weighted[field_index] += value
    if not all(
        math.isfinite(value)
        for value in (net_mass, forward_mass, reverse_mass, *weighted)
    ):
        raise RANSDiagnosticError("measurement integration produced non-finite results")
    if forward_mass <= 0.0:
        raise RANSDiagnosticError("measurement plane has no forward mass flow")
    freestream_total_pressure = freestream_pressure * (
        1.0 + 0.5 * (gamma - 1.0) * freestream_mach_value * freestream_mach_value
    ) ** exponent
    if not math.isfinite(freestream_total_pressure) or freestream_total_pressure <= 0.0:
        raise RANSDiagnosticError("freestream total pressure is invalid")
    weighted_total_pressure = weighted[3] / forward_mass
    return {
        "status": "PASS",
        "name": name,
        "plane_coordinate_m": coordinate,
        "normal_unit": list(normal),
        "slice_component_count": len(components),
        "matching_component_count": 1,
        "selected_cell_count": len(component),
        "selected_component_bounds_m": list(component_bounds),
        "area_m2": component_area,
        "confirmed_geometry_area_m2": expected_area,
        "relative_area_error": relative_area_error,
        "net_mass_flow_kg_s": net_mass,
        "forward_mass_flow_kg_s": forward_mass,
        "reverse_mass_flow_kg_s": reverse_mass,
        "mass_split_closure_error_kg_s": abs(
            (forward_mass - reverse_mass) - net_mass
        ),
        "backflow_mass_fraction": reverse_mass
        / max(forward_mass + reverse_mass, 1.0e-30),
        "mass_weighted_mach": weighted[0] / forward_mass,
        "mass_weighted_static_pressure_pa": weighted[1] / forward_mass,
        "mass_weighted_static_temperature_k": weighted[2] / forward_mass,
        "mass_weighted_total_pressure_pa": weighted_total_pressure,
        "freestream_total_pressure_pa": freestream_total_pressure,
        "total_pressure_recovery": weighted_total_pressure / freestream_total_pressure,
        "mass_flux_source": "Momentum dot confirmed unit normal",
        "integration_convention": "positive mass flux follows confirmed normal",
    }


def _quantile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise RANSDiagnosticError("cannot compute a quantile of no values")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("quantile fraction must be between zero and one")
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return (1.0 - weight) * sorted_values[lower] + weight * sorted_values[upper]


def summarize_wall_yplus(
    mesh: SU2TopologyMesh,
    *,
    solution_points: Sequence[Vector],
    yplus: Sequence[float],
    wall_marker: str,
    target: float,
    maximum: float,
) -> dict[str, Any]:
    """Audit actual ``Y_Plus`` values only on nodes of the named wall marker."""

    target_value = _finite(target, label="target y+")
    maximum_value = _finite(maximum, label="maximum y+")
    if target_value <= 0.0 or maximum_value < target_value:
        raise RANSDiagnosticError("invalid target/maximum y+ contract")
    if wall_marker not in mesh.markers:
        raise RANSDiagnosticError(f"wall marker is absent from mesh: {wall_marker}")
    if len(yplus) != len(solution_points):
        raise RANSDiagnosticError("Y_Plus and solution point counts differ")
    mapping, mapping_evidence = map_mesh_points_to_solution(
        mesh.points, solution_points
    )
    wall_faces = mesh.markers[wall_marker]
    wall_nodes = sorted({node for face in wall_faces for node in face})
    if not wall_faces or not wall_nodes:
        raise RANSDiagnosticError("wall marker has no faces or nodes")

    samples: list[float] = []
    for node in wall_nodes:
        value = _finite(yplus[mapping[node]], label=f"Y_Plus at wall node {node}")
        if value < 0.0:
            raise RANSDiagnosticError(f"Y_Plus is negative at wall node {node}")
        samples.append(value)
    if not any(value > 0.0 for value in samples):
        raise RANSDiagnosticError("all wall Y_Plus samples are zero")

    total_area = 0.0
    integral = 0.0
    above_target_area = 0.0
    above_maximum_area = 0.0
    for face in wall_faces:
        points = tuple(mesh.points[node] for node in face)
        area = _triangle_area(points)
        if not math.isfinite(area) or area <= 0.0:
            raise RANSDiagnosticError(f"wall marker has a degenerate face {face}")
        values = tuple(yplus[mapping[node]] for node in face)
        total_area += area
        integral += area * sum(values) / 3.0
        above_target_area += split_linear_triangle(
            points, values, threshold=target_value
        )["above_area"]
        above_maximum_area += split_linear_triangle(
            points, values, threshold=maximum_value
        )["above_area"]
    if total_area <= 0.0:
        raise RANSDiagnosticError("wall marker has zero total area")

    ordered = sorted(samples)
    maximum_observed = ordered[-1]
    above_maximum_count = sum(value > maximum_value for value in ordered)
    status = (
        "PASS"
        if maximum_observed < maximum_value and above_maximum_area == 0.0
        else "FAIL"
    )
    return {
        "status": status,
        "array_name": "Y_Plus",
        "association": "point",
        "wall_marker": wall_marker,
        "wall_face_count": len(wall_faces),
        "wall_unique_point_count": len(wall_nodes),
        "finite_sample_count": len(samples),
        "positive_sample_count": sum(value > 0.0 for value in samples),
        "minimum": ordered[0],
        "maximum": maximum_observed,
        "mean": sum(ordered) / len(ordered),
        "p50": _quantile(ordered, 0.50),
        "p95": _quantile(ordered, 0.95),
        "p99": _quantile(ordered, 0.99),
        "area_m2": total_area,
        "area_weighted_mean": integral / total_area,
        "target": target_value,
        "maximum_allowed": maximum_value,
        "above_target_point_count": sum(value > target_value for value in ordered),
        "above_target_area_m2": above_target_area,
        "above_target_area_fraction": above_target_area / total_area,
        "above_maximum_point_count": above_maximum_count,
        "at_or_above_maximum_point_count": sum(
            value >= maximum_value for value in ordered
        ),
        "above_maximum_area_m2": above_maximum_area,
        "above_maximum_area_fraction": above_maximum_area / total_area,
        "coordinate_mapping": mapping_evidence,
    }


def _normalized_header(value: str) -> str:
    return value.strip().strip('"').strip()


def summarize_rans_history(
    path: str | Path,
    *,
    freestream_density_kg_m3: float,
    freestream_velocity_m_s: float,
    reference_area_m2: float,
    reference_length_m: float,
    moment_origin_m: Sequence[float],
) -> dict[str, Any]:
    """Read actual SU2 history and report six Cartesian load components."""

    history_path = Path(path).expanduser().resolve(strict=True)
    with history_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise RANSDiagnosticError("history CSV has no data rows")
    raw_headers = [header for header in rows[0] if header is not None]
    normalized = {_normalized_header(header): header for header in raw_headers}
    required = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz")
    missing = [name for name in required if name not in normalized]
    if missing:
        raise RANSDiagnosticError(
            "history lacks Cartesian load coefficients: " + ", ".join(missing)
        )

    coefficients: dict[str, dict[str, float]] = {}
    window_start = max(0, len(rows) - min(10, len(rows)))
    for name in required:
        raw_name = normalized[name]
        values = [
            _finite(row.get(raw_name), label=f"history {name}") for row in rows
        ]
        window = values[window_start:]
        coefficients[name] = {
            "initial": values[0],
            "final": values[-1],
            "last_window_minimum": min(window),
            "last_window_maximum": max(window),
            "last_window_range": max(window) - min(window),
        }

    residuals: dict[str, dict[str, float]] = {}
    for normalized_name, raw_name in normalized.items():
        if not normalized_name.casefold().startswith("rms["):
            continue
        values = [
            _finite(row.get(raw_name), label=f"history {normalized_name}")
            for row in rows
        ]
        residuals[normalized_name] = {
            "initial": values[0],
            "final": values[-1],
            "minimum": min(values),
            "maximum": max(values),
        }
    if not residuals:
        raise RANSDiagnosticError("history has no RMS residual fields")

    density = _finite(freestream_density_kg_m3, label="freestream density")
    velocity = _finite(freestream_velocity_m_s, label="freestream velocity")
    area = _finite(reference_area_m2, label="reference area")
    length = _finite(reference_length_m, label="reference length")
    if min(density, velocity, area, length) <= 0.0:
        raise RANSDiagnosticError("freestream/reference values must be positive")
    if isinstance(moment_origin_m, (str, bytes)) or len(moment_origin_m) != 3:
        raise RANSDiagnosticError("moment origin must have three coordinates")
    origin = [_finite(value, label="moment origin") for value in moment_origin_m]
    dynamic_pressure = 0.5 * density * velocity * velocity
    force_scale = dynamic_pressure * area
    moment_scale = force_scale * length
    dimensional = {
        "Fx_N": coefficients["CFx"]["final"] * force_scale,
        "Fy_N": coefficients["CFy"]["final"] * force_scale,
        "Fz_N": coefficients["CFz"]["final"] * force_scale,
        "Mx_N_m": coefficients["CMx"]["final"] * moment_scale,
        "My_N_m": coefficients["CMy"]["final"] * moment_scale,
        "Mz_N_m": coefficients["CMz"]["final"] * moment_scale,
    }
    if any(not math.isfinite(value) for value in dimensional.values()):
        raise RANSDiagnosticError("dimensional load conversion is non-finite")
    return {
        "status": "PASS",
        "path": str(history_path),
        "row_count": len(rows),
        "headers": list(normalized),
        "coefficients": coefficients,
        "dimensional_loads": dimensional,
        "reference": {
            "dynamic_pressure_pa": dynamic_pressure,
            "reference_area_m2": area,
            "reference_length_m": length,
            "moment_origin_m": origin,
            "force_sign_convention": "+X,+Y,+Z project axes",
            "moment_sign_convention": "right hand rule",
        },
        "residuals": residuals,
        "convergence_claimed": False,
    }


__all__ = [
    "RANSDiagnosticError",
    "summarize_measurement_slice",
    "summarize_rans_history",
    "summarize_wall_yplus",
]
