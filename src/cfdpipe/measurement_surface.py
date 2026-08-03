"""Read-only discovery of coordinate-plane measurement-surface candidates.

The module deliberately separates geometric evidence from project semantics.
It may confirm that a unique geometric section follows a pair of branches, but
it never assigns that section a CFD marker or a business role.  Runtime Gmsh
entity tags are retained only as audit evidence and are excluded from the
stable fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping, Sequence


PathLike = str | os.PathLike[str]
Point3 = tuple[float, float, float]
VolumeMembership = Callable[[Point3], Sequence[int]]

_AXES = ("x", "y", "z")
_CAD_SUFFIXES = {".step", ".stp", ".brep"}
_FINGERPRINT_SCHEMA = "cfdpipe.measurement_surface_fingerprint.v1"
# Imported CAD commonly represents an intended section edge as a B-spline
# with micrometre-scale normal-direction waviness.  Two micrometres is still
# small relative to the discovered sections, remains explicit/auditable in
# the report, and is deliberately separate from endpoint/station tolerances.
_DEFAULT_APPROXIMATE_PLANE_TOLERANCE_M = 2.0e-6


class MeasurementSurfaceError(RuntimeError):
    """Raised when read-only measurement-surface discovery cannot continue."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return bool(attributes & read_only_flag)
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _validate_source(cad_path: PathLike) -> Path:
    candidate = Path(cad_path).expanduser()
    if not candidate.exists():
        raise ValueError(f"CAD path does not exist: {candidate}")
    if candidate.is_symlink():
        raise ValueError(f"measurement discovery refuses a symlink CAD: {candidate}")
    source = candidate.resolve(strict=True)
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"CAD source is not an ordinary file: {source}")
    if source.suffix.lower() not in _CAD_SUFFIXES:
        raise ValueError(f"CAD source must end in .step, .stp or .brep: {source}")
    if not _path_is_read_only(source):
        raise ValueError(f"CAD source must be read-only: {source}")
    return source


def _source_snapshot(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "sha256": _sha256(path),
        "size_bytes": int(metadata.st_size),
        "read_only": _path_is_read_only(path),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MeasurementSurfaceError(f"{label} is boolean, not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise MeasurementSurfaceError(f"{label} is not numeric") from error
    if not math.isfinite(result):
        raise MeasurementSurfaceError(f"{label} contains NaN or Inf")
    return result


def _point(values: Sequence[Any], label: str) -> Point3:
    converted = tuple(_finite_float(value, label) for value in values)
    if len(converted) != 3:
        raise MeasurementSurfaceError(
            f"{label} has {len(converted)} coordinates; expected 3"
        )
    return converted  # type: ignore[return-value]


def _positive_tag(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise MeasurementSurfaceError(f"{label} contains a boolean tag")
    try:
        tag = int(value)
    except (TypeError, ValueError) as error:
        raise MeasurementSurfaceError(f"{label} contains an invalid tag") from error
    if tag <= 0:
        raise MeasurementSurfaceError(f"{label} contains a non-positive tag")
    return tag


def _distance(a: Point3, b: Point3) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b)))


def _flatten_parameter_bound(value: Any, label: str) -> float:
    if isinstance(value, (str, bytes)):
        raise MeasurementSurfaceError(f"{label} is not a numeric sequence")
    try:
        values = list(value)
    except TypeError:
        return _finite_float(value, label)
    if len(values) != 1:
        raise MeasurementSurfaceError(f"{label} must contain exactly one value")
    return _finite_float(values[0], label)


def _sample_curve(gmsh: Any, curve_tag: int, sample_count: int) -> list[Point3]:
    lower_raw, upper_raw = gmsh.model.getParametrizationBounds(1, curve_tag)
    lower = _flatten_parameter_bound(lower_raw, f"curve {curve_tag} lower bound")
    upper = _flatten_parameter_bound(upper_raw, f"curve {curve_tag} upper bound")
    if upper <= lower:
        raise MeasurementSurfaceError(
            f"curve {curve_tag} has invalid parametrization bounds"
        )
    parameters = [
        lower + (upper - lower) * index / (sample_count - 1)
        for index in range(sample_count)
    ]
    values = list(gmsh.model.getValue(1, curve_tag, parameters))
    if len(values) != sample_count * 3:
        raise MeasurementSurfaceError(
            f"curve {curve_tag} returned {len(values)} coordinates; "
            f"expected {sample_count * 3}"
        )
    return [
        _point(values[index : index + 3], f"curve {curve_tag} sample")
        for index in range(0, len(values), 3)
    ]


def _extract_curve_records(gmsh: Any, sample_count: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    entities = sorted(
        (_positive_tag(tag, "curve entities") for _, tag in gmsh.model.getEntities(1))
    )
    if not entities:
        raise MeasurementSurfaceError("STEP contains no curves")
    if len(entities) != len(set(entities)):
        raise MeasurementSurfaceError("STEP contains duplicate curve tags")
    for tag in entities:
        length = _finite_float(gmsh.model.occ.getMass(1, tag), f"curve {tag} length")
        if length <= 0.0:
            raise MeasurementSurfaceError(f"curve {tag} has non-positive length")
        adjacent_raw, _ = gmsh.model.getAdjacencies(1, tag)
        adjacent_surfaces = sorted(
            {
                _positive_tag(value, f"curve {tag} adjacent surfaces")
                for value in adjacent_raw
            }
        )
        records.append(
            {
                "runtime_curve_tag": tag,
                "length_m": length,
                "samples_m": _sample_curve(gmsh, tag, sample_count),
                "adjacent_surface_tags": adjacent_surfaces,
            }
        )
    return records


def _extract_surface_bounds(gmsh: Any) -> dict[int, list[float]]:
    records: dict[int, list[float]] = {}
    for _, raw_tag in gmsh.model.getEntities(2):
        tag = _positive_tag(raw_tag, "surface entities")
        values = [
            _finite_float(value, f"surface {tag} bounds")
            for value in gmsh.model.occ.getBoundingBox(2, tag)
        ]
        if len(values) != 6:
            raise MeasurementSurfaceError(
                f"surface {tag} bounds have {len(values)} values; expected 6"
            )
        if any(values[index] > values[index + 3] for index in range(3)):
            raise MeasurementSurfaceError(f"surface {tag} has inverted bounds")
        records[tag] = values
    return records


def _cluster_endpoint(
    nodes: list[Point3], point: Point3, tolerance_m: float
) -> int:
    matches = [
        (distance, index)
        for index, candidate in enumerate(nodes)
        if (distance := _distance(candidate, point)) <= tolerance_m
    ]
    if not matches:
        nodes.append(point)
        return len(nodes) - 1
    return min(matches)[1]


def _connected_edge_components(edges: Sequence[dict[str, Any]]) -> list[list[int]]:
    node_edges: dict[int, list[int]] = {}
    for index, edge in enumerate(edges):
        node_edges.setdefault(edge["start_node"], []).append(index)
        node_edges.setdefault(edge["end_node"], []).append(index)
    unseen = set(range(len(edges)))
    components: list[list[int]] = []
    while unseen:
        seed = min(unseen)
        stack = [seed]
        component: set[int] = set()
        while stack:
            edge_index = stack.pop()
            if edge_index in component:
                continue
            component.add(edge_index)
            edge = edges[edge_index]
            for node in (edge["start_node"], edge["end_node"]):
                stack.extend(node_edges.get(node, ()))
        unseen.difference_update(component)
        components.append(sorted(component))
    return components


def _ordered_component_points(
    edges: Sequence[dict[str, Any]], component: Sequence[int]
) -> list[Point3] | None:
    degrees: dict[int, int] = {}
    for index in component:
        edge = edges[index]
        degrees[edge["start_node"]] = degrees.get(edge["start_node"], 0) + 1
        degrees[edge["end_node"]] = degrees.get(edge["end_node"], 0) + 1
    if not degrees or any(degree != 2 for degree in degrees.values()):
        return None

    unused = set(component)
    first = min(component)
    first_edge = edges[first]
    current_node = first_edge["start_node"]
    start_node = current_node
    ordered: list[Point3] = []
    while unused:
        candidates = sorted(
            index
            for index in unused
            if current_node in (edges[index]["start_node"], edges[index]["end_node"])
        )
        if not candidates:
            return None
        index = candidates[0]
        edge = edges[index]
        samples = list(edge["record"]["samples_m"])
        if edge["start_node"] == current_node:
            next_node = edge["end_node"]
        else:
            samples.reverse()
            next_node = edge["start_node"]
        if ordered:
            ordered.extend(samples[1:])
        else:
            ordered.extend(samples)
        unused.remove(index)
        current_node = next_node
    if current_node != start_node:
        return None
    if ordered[-1] != ordered[0]:
        ordered.append(ordered[0])
    return ordered


def _projected_polygon_metrics(
    points: Sequence[Point3], axis_index: int
) -> tuple[float, tuple[float, float]]:
    projected_axes = [index for index in range(3) if index != axis_index]
    projected = [(point[projected_axes[0]], point[projected_axes[1]]) for point in points]
    twice_signed_area = 0.0
    centroid_u_numerator = 0.0
    centroid_v_numerator = 0.0
    for (u0, v0), (u1, v1) in zip(projected, projected[1:]):
        cross = u0 * v1 - u1 * v0
        twice_signed_area += cross
        centroid_u_numerator += (u0 + u1) * cross
        centroid_v_numerator += (v0 + v1) * cross
    if abs(twice_signed_area) <= 1.0e-24:
        raise MeasurementSurfaceError("closed curve loop has zero projected area")
    area = abs(twice_signed_area) * 0.5
    centroid = (
        centroid_u_numerator / (3.0 * twice_signed_area),
        centroid_v_numerator / (3.0 * twice_signed_area),
    )
    return area, centroid


def _point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:]):
        if (y0 > y) == (y1 > y):
            continue
        crossing_x = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        if crossing_x > x:
            inside = not inside
    return inside


def _interior_samples(
    points: Sequence[Point3], axis_index: int, centroid: Point3, grid_size: int = 7
) -> list[Point3]:
    projected_axes = [index for index in range(3) if index != axis_index]
    polygon = [(point[projected_axes[0]], point[projected_axes[1]]) for point in points]
    u_values = [value[0] for value in polygon]
    v_values = [value[1] for value in polygon]
    u_min, u_max = min(u_values), max(u_values)
    v_min, v_max = min(v_values), max(v_values)
    plane_coordinate = sum(point[axis_index] for point in points[:-1]) / (len(points) - 1)
    samples: list[Point3] = [centroid]
    for row in range(grid_size):
        v = v_min + (row + 0.5) * (v_max - v_min) / grid_size
        for column in range(grid_size):
            u = u_min + (column + 0.5) * (u_max - u_min) / grid_size
            if not _point_in_polygon((u, v), polygon):
                continue
            coordinates = [0.0, 0.0, 0.0]
            coordinates[axis_index] = plane_coordinate
            coordinates[projected_axes[0]] = u
            coordinates[projected_axes[1]] = v
            candidate = tuple(coordinates)  # type: ignore[assignment]
            if _distance(candidate, centroid) > 1.0e-15:
                samples.append(candidate)
    return samples


def _occupancy_evidence(
    points: Sequence[Point3],
    axis_index: int,
    centroid: Point3,
    volume_membership: VolumeMembership | None,
    minimum_inside_fraction: float,
) -> dict[str, Any]:
    if volume_membership is None:
        return {
            "available": False,
            "qualified": False,
            "sample_count": 0,
            "inside_sample_count": 0,
            "inside_fraction": None,
            "dominant_volume_fraction": None,
            "occupying_volume_count": None,
            "runtime_dominant_volume_tag": None,
        }
    samples = _interior_samples(points, axis_index, centroid)
    volume_counts: dict[int, int] = {}
    inside_count = 0
    overlapping_count = 0
    for sample in samples:
        memberships = sorted(
            {_positive_tag(tag, "volume membership") for tag in volume_membership(sample)}
        )
        if memberships:
            inside_count += 1
        if len(memberships) > 1:
            overlapping_count += 1
        for tag in memberships:
            volume_counts[tag] = volume_counts.get(tag, 0) + 1
    inside_fraction = inside_count / len(samples)
    dominant_tag = max(volume_counts, key=volume_counts.get) if volume_counts else None
    dominant_fraction = (
        0.0 if inside_count == 0 else volume_counts[dominant_tag] / inside_count
    )
    qualified = (
        inside_fraction >= minimum_inside_fraction
        and dominant_fraction >= 0.95
        and overlapping_count == 0
    )
    return {
        "available": True,
        "qualified": qualified,
        "sample_count": len(samples),
        "inside_sample_count": inside_count,
        "inside_fraction": inside_fraction,
        "dominant_volume_fraction": dominant_fraction,
        "occupying_volume_count": len(volume_counts),
        "overlapping_sample_count": overlapping_count,
        "runtime_dominant_volume_tag": dominant_tag,
    }


def _adjacency_evidence(
    incident_surfaces: set[int],
    surface_bounds: Mapping[int, Sequence[float]],
    axis_index: int,
    station: float,
    tolerance_m: float,
) -> dict[str, Any]:
    categories: dict[str, list[int]] = {
        "upstream": [],
        "downstream": [],
        "spanning": [],
        "coplanar": [],
        "unknown": [],
    }
    for tag in sorted(incident_surfaces):
        bounds = surface_bounds.get(tag)
        if bounds is None or len(bounds) != 6:
            categories["unknown"].append(tag)
            continue
        lower = _finite_float(bounds[axis_index], f"surface {tag} lower bound")
        upper = _finite_float(bounds[axis_index + 3], f"surface {tag} upper bound")
        extends_upstream = lower < station - tolerance_m
        extends_downstream = upper > station + tolerance_m
        if extends_upstream and extends_downstream:
            categories["spanning"].append(tag)
        elif extends_upstream:
            categories["upstream"].append(tag)
        elif extends_downstream:
            categories["downstream"].append(tag)
        else:
            categories["coplanar"].append(tag)
    return {
        "incident_surface_count": len(incident_surfaces),
        "upstream_surface_count": len(categories["upstream"]),
        "downstream_surface_count": len(categories["downstream"]),
        "spanning_surface_count": len(categories["spanning"]),
        "coplanar_surface_count": len(categories["coplanar"]),
        "unknown_surface_count": len(categories["unknown"]),
        "runtime_incident_surface_tags": sorted(incident_surfaces),
        "runtime_upstream_surface_tags": categories["upstream"],
        "runtime_downstream_surface_tags": categories["downstream"],
        "runtime_spanning_surface_tags": categories["spanning"],
        "runtime_coplanar_surface_tags": categories["coplanar"],
        "runtime_unknown_surface_tags": categories["unknown"],
    }


def _rounded(value: float) -> float:
    return round(_finite_float(value, "fingerprint value"), 12)


def stable_loop_fingerprint(
    loop: Mapping[str, Any], *, source_sha256: str
) -> dict[str, Any]:
    """Return a tag-independent, source-bound fingerprint for one loop."""

    normalized_hash = str(source_sha256).lower()
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise MeasurementSurfaceError("source_sha256 must be 64 hexadecimal digits")
    adjacency = loop["adjacency_evidence"]
    occupancy = loop["occupancy_evidence"]
    payload = {
        "schema": _FINGERPRINT_SCHEMA,
        "source_sha256": normalized_hash,
        "plane_axis": str(loop["plane_axis"]),
        "plane_coordinate_m": _rounded(loop["plane_coordinate_m"]),
        "centroid_m": [_rounded(value) for value in loop["centroid_m"]],
        "normal_unit": [_rounded(value) for value in loop["normal_unit"]],
        "bounds_m": [_rounded(value) for value in loop["bounds_m"]],
        "curve_count": int(loop["curve_count"]),
        "curve_length_spectrum_m": sorted(
            _rounded(value) for value in loop["curve_length_spectrum_m"]
        ),
        "perimeter_m": _rounded(loop["perimeter_m"]),
        "area_m2": _rounded(loop["area_m2"]),
        "circularity": _rounded(loop["circularity"]),
        "planarity_residual_m": _rounded(loop["planarity_residual_m"]),
        "adjacency_signature": {
            name: int(adjacency[name])
            for name in (
                "incident_surface_count",
                "upstream_surface_count",
                "downstream_surface_count",
                "spanning_surface_count",
                "coplanar_surface_count",
                "unknown_surface_count",
            )
        },
        "occupancy_signature": {
            "available": bool(occupancy["available"]),
            "qualified": bool(occupancy["qualified"]),
            "inside_fraction": (
                None
                if occupancy["inside_fraction"] is None
                else _rounded(occupancy["inside_fraction"])
            ),
            "dominant_volume_fraction": (
                None
                if occupancy["dominant_volume_fraction"] is None
                else _rounded(occupancy["dominant_volume_fraction"])
            ),
            "occupying_volume_count": occupancy["occupying_volume_count"],
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {"payload": payload, "sha256": hashlib.sha256(encoded).hexdigest()}


def enumerate_coordinate_plane_loops(
    curve_records: Sequence[Mapping[str, Any]],
    *,
    surface_bounds: Mapping[int, Sequence[float]],
    source_sha256: str,
    volume_membership: VolumeMembership | None = None,
    plane_tolerance_m: float = _DEFAULT_APPROXIMATE_PLANE_TOLERANCE_M,
    endpoint_tolerance_m: float = 1.0e-8,
    station_tolerance_m: float = 1.0e-7,
    minimum_inside_fraction: float = 0.80,
) -> list[dict[str, Any]]:
    """Enumerate closed loops assembled from curves on coordinate planes.

    This function is pure with respect to Gmsh: callers provide sampled curve
    records and an optional point-to-volume membership callback.
    """

    for value, name in (
        (plane_tolerance_m, "plane_tolerance_m"),
        (endpoint_tolerance_m, "endpoint_tolerance_m"),
        (station_tolerance_m, "station_tolerance_m"),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and greater than zero")
    if not 0.0 < minimum_inside_fraction <= 1.0:
        raise ValueError("minimum_inside_fraction must be in (0, 1]")

    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(curve_records):
        samples = [
            _point(sample, f"curve record {index} sample")
            for sample in raw.get("samples_m", ())
        ]
        if len(samples) < 2:
            raise MeasurementSurfaceError(
                f"curve record {index} must contain at least two samples"
            )
        length = _finite_float(raw.get("length_m"), f"curve record {index} length")
        if length <= 0.0:
            raise MeasurementSurfaceError(
                f"curve record {index} has non-positive length"
            )
        runtime_tag = _positive_tag(
            raw.get("runtime_curve_tag", index + 1), f"curve record {index} tag"
        )
        adjacent = sorted(
            {
                _positive_tag(tag, f"curve record {index} adjacent surfaces")
                for tag in raw.get("adjacent_surface_tags", ())
            }
        )
        validated.append(
            {
                "runtime_curve_tag": runtime_tag,
                "length_m": length,
                "samples_m": samples,
                "adjacent_surface_tags": adjacent,
            }
        )

    planar_items: dict[int, list[tuple[float, dict[str, Any]]]] = {
        index: [] for index in range(3)
    }
    for record in validated:
        for axis_index in range(3):
            coordinates = [point[axis_index] for point in record["samples_m"]]
            residual = max(coordinates) - min(coordinates)
            if residual <= plane_tolerance_m:
                planar_items[axis_index].append(
                    (sum(coordinates) / len(coordinates), record)
                )

    loops: list[dict[str, Any]] = []
    for axis_index, items in planar_items.items():
        remaining = sorted(items, key=lambda item: item[0])
        clusters: list[list[tuple[float, dict[str, Any]]]] = []
        for item in remaining:
            if not clusters:
                clusters.append([item])
                continue
            current_mean = sum(value for value, _ in clusters[-1]) / len(clusters[-1])
            if abs(item[0] - current_mean) <= station_tolerance_m:
                clusters[-1].append(item)
            else:
                clusters.append([item])

        for cluster in clusters:
            nodes: list[Point3] = []
            edges: list[dict[str, Any]] = []
            for _, record in cluster:
                samples = record["samples_m"]
                edges.append(
                    {
                        "record": record,
                        "start_node": _cluster_endpoint(
                            nodes, samples[0], endpoint_tolerance_m
                        ),
                        "end_node": _cluster_endpoint(
                            nodes, samples[-1], endpoint_tolerance_m
                        ),
                    }
                )
            for component in _connected_edge_components(edges):
                points = _ordered_component_points(edges, component)
                if points is None:
                    continue
                area, projected_centroid = _projected_polygon_metrics(
                    points, axis_index
                )
                plane_coordinate = sum(point[axis_index] for point in points[:-1]) / (
                    len(points) - 1
                )
                projected_axes = [index for index in range(3) if index != axis_index]
                centroid_values = [0.0, 0.0, 0.0]
                centroid_values[axis_index] = plane_coordinate
                centroid_values[projected_axes[0]] = projected_centroid[0]
                centroid_values[projected_axes[1]] = projected_centroid[1]
                centroid: Point3 = tuple(centroid_values)  # type: ignore[assignment]
                component_records = [edges[index]["record"] for index in component]
                perimeter = sum(record["length_m"] for record in component_records)
                if perimeter <= 0.0:
                    raise MeasurementSurfaceError("closed loop has invalid perimeter")
                all_points = points[:-1]
                bounds = [
                    min(point[index] for point in all_points) for index in range(3)
                ] + [max(point[index] for point in all_points) for index in range(3)]
                residual = max(
                    abs(point[axis_index] - plane_coordinate) for point in all_points
                )
                incident_surfaces = {
                    tag
                    for record in component_records
                    for tag in record["adjacent_surface_tags"]
                }
                adjacency = _adjacency_evidence(
                    incident_surfaces,
                    surface_bounds,
                    axis_index,
                    plane_coordinate,
                    plane_tolerance_m,
                )
                occupancy = _occupancy_evidence(
                    points,
                    axis_index,
                    centroid,
                    volume_membership,
                    minimum_inside_fraction,
                )
                normal = [0.0, 0.0, 0.0]
                normal[axis_index] = 1.0
                loop: dict[str, Any] = {
                    "plane_axis": _AXES[axis_index],
                    "plane_coordinate_m": plane_coordinate,
                    "centroid_m": list(centroid),
                    "normal_unit": normal,
                    "bounds_m": bounds,
                    "curve_count": len(component_records),
                    "curve_length_spectrum_m": sorted(
                        record["length_m"] for record in component_records
                    ),
                    "perimeter_m": perimeter,
                    "area_m2": area,
                    "circularity": 4.0 * math.pi * area / (perimeter * perimeter),
                    "planarity_residual_m": residual,
                    "closed": True,
                    "runtime_curve_tags": sorted(
                        record["runtime_curve_tag"] for record in component_records
                    ),
                    "adjacency_evidence": adjacency,
                    "occupancy_evidence": occupancy,
                    "topology_evidence": None,
                    "geometry_status": "DISCOVERED_GEOMETRY",
                    "role_status": "UNASSIGNED",
                }
                loop["fingerprint"] = stable_loop_fingerprint(
                    loop, source_sha256=source_sha256
                )
                loops.append(loop)

    loops.sort(
        key=lambda item: (
            _AXES.index(item["plane_axis"]),
            item["plane_coordinate_m"],
            item["centroid_m"],
            item["fingerprint"]["sha256"],
        )
    )
    return loops


def classify_branch_merge_candidates(
    loops: Sequence[dict[str, Any]],
    *,
    flow_axis: str = "x",
    station_tolerance_m: float = 1.0e-7,
) -> dict[str, Any]:
    """Classify, but do not name, a unique two-branch-to-one section."""

    normalized_axis = str(flow_axis).lower()
    if normalized_axis not in _AXES:
        raise ValueError("flow_axis must be x, y or z")
    if not math.isfinite(station_tolerance_m) or station_tolerance_m <= 0.0:
        raise ValueError("station_tolerance_m must be finite and greater than zero")

    eligible = [
        loop
        for loop in loops
        if loop["plane_axis"] == normalized_axis
        and loop["occupancy_evidence"]["qualified"]
        and (
            loop["adjacency_evidence"]["upstream_surface_count"]
            + loop["adjacency_evidence"]["spanning_surface_count"]
            > 0
        )
        and (
            loop["adjacency_evidence"]["downstream_surface_count"]
            + loop["adjacency_evidence"]["spanning_surface_count"]
            > 0
        )
    ]
    stations: list[list[dict[str, Any]]] = []
    for loop in sorted(eligible, key=lambda item: item["plane_coordinate_m"]):
        if not stations:
            stations.append([loop])
            continue
        current_mean = sum(
            item["plane_coordinate_m"] for item in stations[-1]
        ) / len(stations[-1])
        if abs(loop["plane_coordinate_m"] - current_mean) <= station_tolerance_m:
            stations[-1].append(loop)
        else:
            stations.append([loop])

    station_evidence: list[dict[str, Any]] = []
    for index, station_loops in enumerate(stations):
        coordinate = sum(
            item["plane_coordinate_m"] for item in station_loops
        ) / len(station_loops)
        record = {
            "plane_axis": normalized_axis,
            "plane_coordinate_m": coordinate,
            "connected_loop_count": len(station_loops),
            "loop_fingerprint_sha256": sorted(
                item["fingerprint"]["sha256"] for item in station_loops
            ),
            "upstream_station_coordinate_m": (
                None
                if index == 0
                else sum(
                    item["plane_coordinate_m"] for item in stations[index - 1]
                )
                / len(stations[index - 1])
            ),
            "upstream_connected_loop_count": (
                None if index == 0 else len(stations[index - 1])
            ),
            "downstream_station_coordinate_m": (
                None
                if index + 1 == len(stations)
                else sum(
                    item["plane_coordinate_m"] for item in stations[index + 1]
                )
                / len(stations[index + 1])
            ),
            "downstream_connected_loop_count": (
                None if index + 1 == len(stations) else len(stations[index + 1])
            ),
        }
        station_evidence.append(record)
        for loop in station_loops:
            loop["topology_evidence"] = dict(record)

    transitions: list[dict[str, Any]] = []
    for index in range(1, len(stations)):
        if len(stations[index - 1]) == 2 and len(stations[index]) == 1:
            loop = stations[index][0]
            transitions.append(
                {
                    "basis": "first_single_connected_section_after_paired_branches",
                    "upstream_station_coordinate_m": station_evidence[index - 1][
                        "plane_coordinate_m"
                    ],
                    "upstream_connected_loop_count": 2,
                    "candidate_station_coordinate_m": station_evidence[index][
                        "plane_coordinate_m"
                    ],
                    "candidate_connected_loop_count": 1,
                    "candidate_fingerprint_sha256": loop["fingerprint"]["sha256"],
                }
            )

    if len(transitions) == 1:
        selected_hash = transitions[0]["candidate_fingerprint_sha256"]
        selected = next(
            loop for loop in loops if loop["fingerprint"]["sha256"] == selected_hash
        )
        selected["geometry_status"] = "CONFIRMED_GEOMETRY"
        selected["role_status"] = "CANDIDATE_ROLE"
        geometry_status = "CONFIRMED_GEOMETRY"
        role_status = "CANDIDATE_ROLE"
    elif transitions:
        selected_hash = None
        geometry_status = "AMBIGUOUS_GEOMETRY"
        role_status = "UNRESOLVED_ROLE"
    else:
        selected_hash = None
        geometry_status = "NO_UNIQUE_TRANSITION"
        role_status = "UNRESOLVED_ROLE"

    return {
        "flow_axis": normalized_axis,
        "basis": "first_single_connected_section_after_paired_branches",
        "business_role_asserted": False,
        "geometry_status": geometry_status,
        "role_status": role_status,
        "selected_fingerprint_sha256": selected_hash,
        "stations": station_evidence,
        "transition_candidates": transitions,
    }


def inspect_measurement_surfaces(
    step_path: PathLike,
    *,
    output_path: PathLike | None = None,
    flow_axis: str = "x",
    curve_sample_count: int = 65,
    plane_tolerance_m: float = _DEFAULT_APPROXIMATE_PLANE_TOLERANCE_M,
    endpoint_tolerance_m: float = 1.0e-8,
    station_tolerance_m: float = 1.0e-7,
    minimum_inside_fraction: float = 0.80,
) -> dict[str, Any]:
    """Inspect a read-only STEP/BREP using Gmsh OCC without modifying or meshing."""

    if (
        isinstance(curve_sample_count, bool)
        or not isinstance(curve_sample_count, int)
        or curve_sample_count < 5
    ):
        raise ValueError("curve_sample_count must be an integer of at least 5")
    normalized_axis = str(flow_axis).lower()
    if normalized_axis not in _AXES:
        raise ValueError("flow_axis must be x, y or z")

    source = _validate_source(step_path)
    output: Path | None = None
    if output_path is not None:
        output = Path(output_path).expanduser().resolve(strict=False)
        if output.suffix.lower() != ".json":
            raise MeasurementSurfaceError(
                "measurement-surface evidence output must be a .json file"
            )
        if output.exists():
            raise MeasurementSurfaceError(
                f"refusing to overwrite measurement-surface evidence: {output}"
            )
        if output == source:
            raise MeasurementSurfaceError("evidence output must not be the CAD source")
    before = _source_snapshot(source)
    report: dict[str, Any] | None = None
    try:
        import gmsh

        gmsh.initialize()
        try:
            gmsh.model.add("cfdpipe_measurement_surface_discovery")
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            imported = gmsh.model.occ.importShapes(str(source))
            gmsh.model.occ.synchronize()
            if not imported:
                raise MeasurementSurfaceError(f"Gmsh imported no entities from {source}")

            curve_records = _extract_curve_records(gmsh, curve_sample_count)
            surface_bounds = _extract_surface_bounds(gmsh)
            volume_tags = sorted(
                _positive_tag(tag, "volume entities")
                for _, tag in gmsh.model.getEntities(3)
            )
            is_inside = getattr(gmsh.model, "isInside", None)
            membership: VolumeMembership | None = None
            if volume_tags and callable(is_inside):
                def membership(point: Point3) -> Sequence[int]:
                    return [
                        tag
                        for tag in volume_tags
                        if int(is_inside(3, tag, list(point))) > 0
                    ]

            loops = enumerate_coordinate_plane_loops(
                curve_records,
                surface_bounds=surface_bounds,
                source_sha256=before["sha256"],
                volume_membership=membership,
                plane_tolerance_m=plane_tolerance_m,
                endpoint_tolerance_m=endpoint_tolerance_m,
                station_tolerance_m=station_tolerance_m,
                minimum_inside_fraction=minimum_inside_fraction,
            )
            classification = classify_branch_merge_candidates(
                loops,
                flow_axis=normalized_axis,
                station_tolerance_m=station_tolerance_m,
            )
            report = {
                "status": "PASS",
                "source_cad_path": str(source),
                "source_cad_format": source.suffix.lower().lstrip("."),
                "derived_cad_sha256": before["sha256"],
                "source_sha256_before": before["sha256"],
                "source_sha256_after": None,
                "source_read_only_before": before["read_only"],
                "source_read_only_after": None,
                "source_unchanged": None,
                "gmsh_version": str(
                    getattr(gmsh, "__version__", getattr(gmsh, "GMSH_API_VERSION", "unknown"))
                ),
                "gmsh_module_path": (
                    None
                    if getattr(gmsh, "__file__", None) is None
                    else str(Path(gmsh.__file__).resolve())
                ),
                "target_geometry_unit": "M",
                "curve_sample_count": curve_sample_count,
                "discovery_parameters": {
                    "flow_axis": normalized_axis,
                    "plane_tolerance_m": plane_tolerance_m,
                    "endpoint_tolerance_m": endpoint_tolerance_m,
                    "station_tolerance_m": station_tolerance_m,
                    "minimum_inside_fraction": minimum_inside_fraction,
                },
                "loop_count": len(loops),
                "loops": loops,
                "classification": classification,
                "operations": {
                    "mesh_generated": False,
                    "file_written_by_gmsh": False,
                    "physical_groups_created": False,
                    "source_geometry_modified": False,
                },
            }
        finally:
            gmsh.finalize()
    finally:
        after = _source_snapshot(source)
        unchanged = (
            after["sha256"] == before["sha256"]
            and after["size_bytes"] == before["size_bytes"]
            and before["read_only"]
            and after["read_only"]
        )
        if report is not None:
            report["source_sha256_after"] = after["sha256"]
            report["source_read_only_after"] = after["read_only"]
            report["source_unchanged"] = unchanged
        if not unchanged:
            raise MeasurementSurfaceError(
                "source STEP hash, size or read-only state changed during discovery"
            )

    assert report is not None
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return report


__all__ = [
    "MeasurementSurfaceError",
    "classify_branch_merge_candidates",
    "enumerate_coordinate_plane_loops",
    "inspect_measurement_surfaces",
    "stable_loop_fingerprint",
]
