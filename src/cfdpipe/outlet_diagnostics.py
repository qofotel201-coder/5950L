"""Pure-Python topology and flux calculations for outlet diagnostics.

ParaView data extraction stays in the ``pvbatch`` entry point.  Keeping the
geometry and integration logic here makes it testable by ordinary Python and
does not import ``paraview.simple``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


class OutletDiagnosticError(RuntimeError):
    """Raised when mesh/solution evidence is incomplete or ambiguous."""


Vector = tuple[float, float, float]


@dataclass(frozen=True)
class SU2TopologyMesh:
    path: Path
    sha256: str
    points: dict[int, Vector]
    tetrahedra: tuple[tuple[int, int, int, int], ...]
    prisms: tuple[tuple[int, int, int, int, int, int], ...]
    markers: dict[str, tuple[tuple[int, int, int], ...]]


VolumeElement = tuple[int, ...]
FaceKey = tuple[int, ...]


def _element_faces(element_type: int, nodes: VolumeElement) -> tuple[FaceKey, ...]:
    if element_type == 10:
        a, b, c, d = nodes
        return (
            (a, b, c),
            (a, b, d),
            (a, c, d),
            (b, c, d),
        )
    if element_type == 13:
        a, b, c, d, e, f = nodes
        return (
            (a, b, c),
            (d, e, f),
            (a, b, e, d),
            (b, c, f, e),
            (c, a, d, f),
        )
    raise OutletDiagnosticError(f"unsupported volume element type {element_type}")


def _volume_face_owners(
    tetrahedra: Sequence[tuple[int, int, int, int]],
    prisms: Sequence[tuple[int, int, int, int, int, int]],
) -> dict[FaceKey, list[VolumeElement]]:
    owners: dict[FaceKey, list[VolumeElement]] = {}
    for element_type, elements in ((10, tetrahedra), (13, prisms)):
        for element in elements:
            for face in _element_faces(element_type, element):
                owners.setdefault(tuple(sorted(face)), []).append(element)
    return owners


def _validate_exterior_marker_coverage(
    tetrahedra: Sequence[tuple[int, int, int, int]],
    prisms: Sequence[tuple[int, int, int, int, int, int]],
    markers: Mapping[str, Sequence[tuple[int, int, int]]],
) -> None:
    owners = _volume_face_owners(tetrahedra, prisms)
    nonmanifold = sorted(face for face, entries in owners.items() if len(entries) > 2)
    if nonmanifold:
        raise OutletDiagnosticError(
            f"volume mesh has non-manifold face ownership: {nonmanifold[0]}"
        )

    exterior = {face for face, entries in owners.items() if len(entries) == 1}
    exterior_quadrilaterals = sorted(face for face in exterior if len(face) == 4)
    if exterior_quadrilaterals:
        raise OutletDiagnosticError(
            "volume mesh has an exterior quadrilateral that cannot be covered by "
            f"the required type-5 triangle markers: {exterior_quadrilaterals[0]}"
        )
    unsupported = sorted(face for face in exterior if len(face) != 3)
    if unsupported:
        raise OutletDiagnosticError(
            f"volume mesh has an unsupported exterior face: {unsupported[0]}"
        )

    marker_faces: set[FaceKey] = set()
    for marker, faces in markers.items():
        for face in faces:
            key = tuple(sorted(face))
            if key in marker_faces:
                raise OutletDiagnosticError(
                    f"duplicate boundary face {key} in marker {marker}"
                )
            marker_faces.add(key)
    if marker_faces != exterior:
        missing = sorted(exterior - marker_faces)
        extra = sorted(marker_faces - exterior)
        raise OutletDiagnosticError(
            "type-5 markers do not exactly cover all exterior volume faces; "
            f"missing={missing[:1]}, extra={extra[:1]}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_float(raw: str, *, label: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise OutletDiagnosticError(f"invalid {label}: {raw!r}") from error
    if not math.isfinite(value):
        raise OutletDiagnosticError(f"non-finite {label}: {raw!r}")
    return value


def _assignment(line: str, expected: str) -> str:
    if "=" not in line:
        raise OutletDiagnosticError(f"expected {expected}= assignment, got {line!r}")
    key, value = line.split("=", 1)
    if key.strip().upper() != expected:
        raise OutletDiagnosticError(f"expected {expected}= assignment, got {line!r}")
    return value.strip()


def _positive_count(raw: str, *, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise OutletDiagnosticError(f"invalid {label}: {raw!r}") from error
    if value <= 0:
        raise OutletDiagnosticError(f"{label} must be greater than zero")
    return value


def parse_topology_smoke_su2(path: str | Path) -> SU2TopologyMesh:
    """Parse the supported Tet4/Prism6 and triangle-marker SU2 subset."""

    mesh_path = Path(path).expanduser().resolve(strict=True)
    if not mesh_path.is_file() or mesh_path.stat().st_size <= 0:
        raise OutletDiagnosticError(f"mesh is missing or empty: {mesh_path}")
    lines = [
        line.split("%", 1)[0].strip()
        for line in mesh_path.read_text(encoding="utf-8-sig").splitlines()
        if line.split("%", 1)[0].strip()
    ]
    cursor = 0
    if _positive_count(_assignment(lines[cursor], "NDIME"), label="NDIME") != 3:
        raise OutletDiagnosticError("outlet diagnostics require NDIME=3")
    cursor += 1
    element_count = _positive_count(
        _assignment(lines[cursor], "NELEM"), label="NELEM"
    )
    cursor += 1
    tetrahedra: list[tuple[int, int, int, int]] = []
    prisms: list[tuple[int, int, int, int, int, int]] = []
    for element_index in range(element_count):
        if cursor >= len(lines):
            raise OutletDiagnosticError("mesh ended inside NELEM section")
        fields = lines[cursor].split()
        cursor += 1
        try:
            element_type = int(fields[0])
        except (IndexError, ValueError) as error:
            raise OutletDiagnosticError(
                f"invalid volume element at index {element_index}"
            ) from error
        node_count = {10: 4, 13: 6}.get(element_type)
        if node_count is None or len(fields) < node_count + 1:
            raise OutletDiagnosticError(
                "outlet diagnostics require type-10 Tet4 or type-13 Prism6 "
                f"volume elements; got type {element_type}"
            )
        try:
            nodes = tuple(int(value) for value in fields[1 : node_count + 1])
        except ValueError as error:
            raise OutletDiagnosticError(
                f"invalid volume-element node at index {element_index}"
            ) from error
        if len(set(nodes)) != node_count:
            raise OutletDiagnosticError(
                f"degenerate type-{element_type} element at index {element_index}"
            )
        if element_type == 10:
            tetrahedra.append(nodes)  # type: ignore[arg-type]
        else:
            prisms.append(nodes)  # type: ignore[arg-type]

    if cursor >= len(lines):
        raise OutletDiagnosticError("mesh lacks NPOIN section")
    point_count = _positive_count(_assignment(lines[cursor], "NPOIN"), label="NPOIN")
    cursor += 1
    points: dict[int, Vector] = {}
    for point_index in range(point_count):
        if cursor >= len(lines):
            raise OutletDiagnosticError("mesh ended inside NPOIN section")
        fields = lines[cursor].split()
        cursor += 1
        if len(fields) < 3:
            raise OutletDiagnosticError(f"invalid point at index {point_index}")
        coordinates = tuple(
            _finite_float(value, label=f"point {point_index} coordinate")
            for value in fields[:3]
        )
        try:
            point_id = point_index if len(fields) < 4 else int(fields[-1])
        except ValueError as error:
            raise OutletDiagnosticError(f"invalid point id at index {point_index}") from error
        if point_id in points:
            raise OutletDiagnosticError(f"duplicate point id {point_id}")
        points[point_id] = coordinates  # type: ignore[assignment]

    if cursor >= len(lines):
        raise OutletDiagnosticError("mesh lacks NMARK section")
    marker_count = _positive_count(_assignment(lines[cursor], "NMARK"), label="NMARK")
    cursor += 1
    markers: dict[str, tuple[tuple[int, int, int], ...]] = {}
    for _ in range(marker_count):
        if cursor + 1 >= len(lines):
            raise OutletDiagnosticError("mesh ended inside marker header")
        marker = _assignment(lines[cursor], "MARKER_TAG")
        cursor += 1
        if not marker or marker in markers:
            raise OutletDiagnosticError(f"invalid or duplicate marker {marker!r}")
        face_count = _positive_count(
            _assignment(lines[cursor], "MARKER_ELEMS"), label="MARKER_ELEMS"
        )
        cursor += 1
        faces: list[tuple[int, int, int]] = []
        for face_index in range(face_count):
            if cursor >= len(lines):
                raise OutletDiagnosticError(f"mesh ended inside marker {marker}")
            fields = lines[cursor].split()
            cursor += 1
            if len(fields) < 4 or fields[0] != "5":
                raise OutletDiagnosticError(
                    "outlet diagnostics currently require type-5 boundary triangles only"
                )
            try:
                face = tuple(int(value) for value in fields[1:4])
            except ValueError as error:
                raise OutletDiagnosticError(
                    f"invalid triangle {face_index} in marker {marker}"
                ) from error
            if len(set(face)) != 3:
                raise OutletDiagnosticError(
                    f"degenerate triangle {face_index} in marker {marker}"
                )
            faces.append(face)  # type: ignore[arg-type]
        markers[marker] = tuple(faces)

    if cursor != len(lines):
        raise OutletDiagnosticError("unexpected trailing SU2 mesh content")
    point_ids = set(points)
    for element in tetrahedra:
        if not set(element).issubset(point_ids):
            raise OutletDiagnosticError("tetrahedron references an unknown point")
    for element in prisms:
        if not set(element).issubset(point_ids):
            raise OutletDiagnosticError("prism references an unknown point")
    for marker, faces in markers.items():
        if any(not set(face).issubset(point_ids) for face in faces):
            raise OutletDiagnosticError(f"marker {marker} references an unknown point")
    _validate_exterior_marker_coverage(tetrahedra, prisms, markers)
    return SU2TopologyMesh(
        path=mesh_path,
        sha256=_sha256(mesh_path),
        points=points,
        tetrahedra=tuple(tetrahedra),
        prisms=tuple(prisms),
        markers=markers,
    )


def _sub(left: Vector, right: Vector) -> Vector:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _add(left: Vector, right: Vector) -> Vector:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _scale(value: Vector, factor: float) -> Vector:
    return tuple(component * factor for component in value)  # type: ignore[return-value]


def _dot(left: Vector, right: Vector) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: Vector, right: Vector) -> Vector:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: Vector) -> float:
    return math.sqrt(_dot(value, value))


def _triangle_area(points: Sequence[Vector]) -> float:
    return 0.5 * _norm(_cross(_sub(points[1], points[0]), _sub(points[2], points[0])))


def _clip_linear_polygon(
    vertices: Sequence[tuple[Vector, float]],
    *,
    threshold: float,
    keep_below: bool,
) -> list[tuple[Vector, float]]:
    result: list[tuple[Vector, float]] = []
    for current, following in zip(vertices, (*vertices[1:], vertices[0]), strict=True):
        current_inside = current[1] <= threshold if keep_below else current[1] >= threshold
        following_inside = following[1] <= threshold if keep_below else following[1] >= threshold
        if current_inside:
            result.append(current)
        if current_inside != following_inside:
            denominator = following[1] - current[1]
            if denominator == 0.0:
                raise OutletDiagnosticError("invalid zero-crossing interpolation")
            fraction = (threshold - current[1]) / denominator
            point = _add(current[0], _scale(_sub(following[0], current[0]), fraction))
            result.append((point, threshold))
    return result


def _polygon_area_and_integral(vertices: Sequence[tuple[Vector, float]]) -> tuple[float, float]:
    if len(vertices) < 3:
        return 0.0, 0.0
    area = 0.0
    integral = 0.0
    for index in range(1, len(vertices) - 1):
        triangle = (vertices[0], vertices[index], vertices[index + 1])
        triangle_area = _triangle_area(tuple(item[0] for item in triangle))
        area += triangle_area
        integral += triangle_area * sum(item[1] for item in triangle) / 3.0
    return area, integral


def split_linear_triangle(
    points: Sequence[Vector],
    values: Sequence[float],
    *,
    threshold: float = 0.0,
) -> dict[str, float]:
    """Split a linearly interpolated triangular scalar at ``threshold``."""

    if len(points) != 3 or len(values) != 3:
        raise OutletDiagnosticError("triangle split requires three points and values")
    if any(not math.isfinite(value) for value in values) or not math.isfinite(threshold):
        raise OutletDiagnosticError("triangle split values must be finite")
    vertices = list(zip(points, values, strict=True))
    below = _clip_linear_polygon(vertices, threshold=threshold, keep_below=True)
    above = _clip_linear_polygon(vertices, threshold=threshold, keep_below=False)
    below_area, below_integral = _polygon_area_and_integral(below)
    above_area, above_integral = _polygon_area_and_integral(above)
    return {
        "below_area": below_area,
        "below_integral": below_integral,
        "above_area": above_area,
        "above_integral": above_integral,
    }


def map_mesh_points_to_solution(
    mesh_points: Mapping[int, Vector],
    solution_points: Sequence[Vector],
) -> tuple[dict[int, int], dict[str, Any]]:
    """Create a unique coordinate-verified mesh-node to solution-point map."""

    if len(mesh_points) != len(solution_points) or not mesh_points:
        raise OutletDiagnosticError("mesh and solution point counts do not match")
    all_coordinates = [*mesh_points.values(), *solution_points]
    bounds = [
        (min(point[axis] for point in all_coordinates), max(point[axis] for point in all_coordinates))
        for axis in range(3)
    ]
    diagonal = math.sqrt(sum((maximum - minimum) ** 2 for minimum, maximum in bounds))
    tolerance = max(5.0e-7 * max(diagonal, 1.0), 1.0e-9)

    ordered_ids = sorted(mesh_points)
    if ordered_ids == list(range(len(solution_points))):
        errors = [_norm(_sub(mesh_points[index], solution_points[index])) for index in ordered_ids]
        if max(errors, default=0.0) <= tolerance:
            return (
                {index: index for index in ordered_ids},
                {
                    "mode": "identity_verified_by_coordinates",
                    "tolerance_m": tolerance,
                    "maximum_error_m": max(errors, default=0.0),
                    "unmatched_points": 0,
                    "ambiguous_points": 0,
                },
            )

    def bucket(point: Vector) -> tuple[int, int, int]:
        return tuple(math.floor(value / tolerance) for value in point)  # type: ignore[return-value]

    buckets: dict[tuple[int, int, int], list[int]] = {}
    for solution_index, point in enumerate(solution_points):
        buckets.setdefault(bucket(point), []).append(solution_index)
    mapping: dict[int, int] = {}
    used: set[int] = set()
    maximum_error = 0.0
    for point_id in ordered_ids:
        point = mesh_points[point_id]
        key = bucket(point)
        candidates: list[tuple[float, int]] = []
        for offsets in itertools.product((-1, 0, 1), repeat=3):
            neighbor = tuple(key[axis] + offsets[axis] for axis in range(3))
            for solution_index in buckets.get(neighbor, ()):
                distance = _norm(_sub(point, solution_points[solution_index]))
                if distance <= tolerance:
                    candidates.append((distance, solution_index))
        candidates.sort()
        unique = [(distance, index) for distance, index in candidates if index not in used]
        if len(unique) != 1:
            label = "unmatched" if not unique else "ambiguous"
            raise OutletDiagnosticError(f"{label} coordinate mapping for mesh point {point_id}")
        distance, solution_index = unique[0]
        mapping[point_id] = solution_index
        used.add(solution_index)
        maximum_error = max(maximum_error, distance)
    if len(used) != len(solution_points):
        raise OutletDiagnosticError("coordinate mapping is not one-to-one")
    return mapping, {
        "mode": "unique_coordinate_match",
        "tolerance_m": tolerance,
        "maximum_error_m": maximum_error,
        "unmatched_points": 0,
        "ambiguous_points": 0,
    }


def _face_owners(mesh: SU2TopologyMesh) -> dict[FaceKey, list[VolumeElement]]:
    return _volume_face_owners(mesh.tetrahedra, mesh.prisms)


def analyze_outlets(
    mesh: SU2TopologyMesh,
    *,
    solution_points: Sequence[Vector],
    density: Sequence[float],
    momentum: Sequence[Vector],
    velocity: Sequence[Vector],
    mach: Sequence[float],
    outlet_markers: Sequence[str],
    farfield_marker: str,
    wall_marker: str,
) -> dict[str, Any]:
    """Integrate marker-resolved fluxes using oriented boundary triangles."""

    point_count = len(solution_points)
    arrays = (density, momentum, velocity, mach)
    if any(len(array) != point_count for array in arrays):
        raise OutletDiagnosticError("solution point arrays have inconsistent lengths")
    if isinstance(outlet_markers, (str, bytes)) or len(outlet_markers) != 2:
        raise OutletDiagnosticError("exactly two outlet marker names are required")
    required_markers = {farfield_marker, wall_marker, *outlet_markers}
    if required_markers != set(mesh.markers):
        missing = sorted(required_markers - set(mesh.markers))
        extra = sorted(set(mesh.markers) - required_markers)
        raise OutletDiagnosticError(
            f"solver marker set mismatch; missing={missing}, extra={extra}"
        )
    for index in range(point_count):
        scalar_values = (density[index], mach[index], *momentum[index], *velocity[index])
        if any(not math.isfinite(value) for value in scalar_values):
            raise OutletDiagnosticError(f"solution contains non-finite values at point {index}")
        if density[index] <= 0.0 or mach[index] < 0.0:
            raise OutletDiagnosticError(f"solution has invalid density/Mach at point {index}")

    point_mapping, mapping_evidence = map_mesh_points_to_solution(
        mesh.points, solution_points
    )
    momentum_consistency = 0.0
    for index in range(point_count):
        expected = _scale(velocity[index], density[index])
        difference = _norm(_sub(momentum[index], expected))
        scale = max(_norm(momentum[index]), _norm(expected), 1.0e-30)
        momentum_consistency = max(momentum_consistency, difference / scale)
    if momentum_consistency > 1.0e-4:
        raise OutletDiagnosticError(
            "Momentum is inconsistent with Density*Velocity beyond 1e-4"
        )

    owners = _face_owners(mesh)
    invalid_owner_keys = {
        face for face, entries in owners.items() if len(entries) not in (1, 2)
    }
    if invalid_owner_keys:
        raise OutletDiagnosticError(
            "volume mesh contains a face with invalid owner count"
        )
    exterior_keys = {face for face, entries in owners.items() if len(entries) == 1}
    exterior_quadrilaterals = {face for face in exterior_keys if len(face) == 4}
    if exterior_quadrilaterals:
        raise OutletDiagnosticError(
            "volume mesh contains an exterior quadrilateral without a type-5 marker"
        )
    exterior_triangles = {face for face in exterior_keys if len(face) == 3}
    marker_keys: set[tuple[int, int, int]] = set()
    marker_results: dict[str, dict[str, Any]] = {}
    for marker, faces in mesh.markers.items():
        evaluate_normal_mach = marker != wall_marker
        normal_mach_faces: list[dict[str, Any]] = []
        totals = {
            "face_count": len(faces),
            "area_m2": 0.0,
            "outward_normal_flip_count": 0,
            "net_mass_flow_kg_s": 0.0,
            "forward_mass_flow_kg_s": 0.0,
            "reverse_mass_flow_kg_s": 0.0,
            "backflow_area_m2": 0.0,
            "subsonic_normal_area_m2": 0.0,
            "nonpositive_normal_mach_area_m2": 0.0,
            "normal_mach_area_integral": 0.0,
            "normal_mach_vertex_min": math.inf,
            "normal_mach_vertex_max": -math.inf,
            "normal_mach_face_centroid_min": math.inf,
            "normal_mach_face_centroid_max": -math.inf,
            "zero_speed_normal_mach_vertex_sample_count": 0,
            "zero_speed_normal_mach_face_count": 0,
        }
        for face_index, face in enumerate(faces):
            key = tuple(sorted(face))
            if key in marker_keys:
                raise OutletDiagnosticError(f"duplicate boundary face {key}")
            marker_keys.add(key)
            owner_elements = owners.get(key, ())
            if len(owner_elements) != 1:
                raise OutletDiagnosticError(
                    f"marker {marker} face {face_index} has {len(owner_elements)} owners"
                )
            points = tuple(mesh.points[node] for node in face)
            area_vector = _scale(
                _cross(_sub(points[1], points[0]), _sub(points[2], points[0])),
                0.5,
            )
            area = _norm(area_vector)
            if not math.isfinite(area) or area <= 0.0:
                raise OutletDiagnosticError(f"degenerate boundary face {key}")
            centroid = _scale(_add(_add(points[0], points[1]), points[2]), 1.0 / 3.0)
            owner_nodes = owner_elements[0]
            owner_centroid = tuple(
                sum(mesh.points[node][axis] for node in owner_nodes) / len(owner_nodes)
                for axis in range(3)
            )
            if _dot(area_vector, _sub(owner_centroid, centroid)) > 0.0:
                area_vector = _scale(area_vector, -1.0)
                totals["outward_normal_flip_count"] += 1
            normal = _scale(area_vector, 1.0 / area)
            solution_indices = tuple(point_mapping[node] for node in face)
            mass_flux_values = tuple(
                _dot(momentum[index], normal) for index in solution_indices
            )
            signed_flux = area * sum(mass_flux_values) / 3.0
            mass_split = split_linear_triangle(points, mass_flux_values)
            forward = max(0.0, mass_split["above_integral"])
            reverse = max(0.0, -mass_split["below_integral"])
            if abs((forward - reverse) - signed_flux) > 1.0e-9 * max(
                abs(signed_flux), forward + reverse, 1.0
            ):
                raise OutletDiagnosticError(f"mass split does not close on face {key}")

            totals["area_m2"] += area
            totals["net_mass_flow_kg_s"] += signed_flux
            totals["forward_mass_flow_kg_s"] += forward
            totals["reverse_mass_flow_kg_s"] += reverse
            if evaluate_normal_mach:
                normal_mach_values: list[float] = []
                face_has_zero_speed_sample = False
                for index in solution_indices:
                    speed = _norm(velocity[index])
                    if speed <= 0.0:
                        # A no-slip/open-marker junction can legitimately carry
                        # the wall value at the shared vertex.  Its limiting
                        # normal Mach is zero; retain that conservative sample
                        # and expose the count instead of dividing by zero.
                        normal_mach_values.append(0.0)
                        totals["zero_speed_normal_mach_vertex_sample_count"] += 1
                        face_has_zero_speed_sample = True
                    else:
                        normal_mach_values.append(
                            mach[index] * _dot(velocity[index], normal) / speed
                        )
                if face_has_zero_speed_sample:
                    totals["zero_speed_normal_mach_face_count"] += 1
                normal_mach = tuple(normal_mach_values)
                subsonic = split_linear_triangle(points, normal_mach, threshold=1.0)
                nonpositive = split_linear_triangle(points, normal_mach, threshold=0.0)
                face_centroid_mach = sum(normal_mach) / 3.0
                normal_mach_faces.append(
                    {
                        "marker_face_index": face_index,
                        "mesh_node_ids": list(face),
                        "centroid_m": list(centroid),
                        "outward_normal": list(normal),
                        "area_m2": area,
                        "vertex_normal_mach": list(normal_mach),
                        "face_centroid_normal_mach": face_centroid_mach,
                        "subsonic_area_m2": subsonic["below_area"],
                    }
                )
                totals["backflow_area_m2"] += mass_split["below_area"]
                totals["subsonic_normal_area_m2"] += subsonic["below_area"]
                totals["nonpositive_normal_mach_area_m2"] += nonpositive["below_area"]
                totals["normal_mach_area_integral"] += area * face_centroid_mach
                totals["normal_mach_vertex_min"] = min(
                    totals["normal_mach_vertex_min"], *normal_mach
                )
                totals["normal_mach_vertex_max"] = max(
                    totals["normal_mach_vertex_max"], *normal_mach
                )
                totals["normal_mach_face_centroid_min"] = min(
                    totals["normal_mach_face_centroid_min"], face_centroid_mach
                )
                totals["normal_mach_face_centroid_max"] = max(
                    totals["normal_mach_face_centroid_max"], face_centroid_mach
                )

        area = totals.pop("area_m2")
        normal_mach_integral = totals.pop("normal_mach_area_integral")
        forward = totals["forward_mass_flow_kg_s"]
        reverse = totals["reverse_mass_flow_kg_s"]
        totals.update(
            {
                "area_m2": area,
                "normal_mach_evaluated": evaluate_normal_mach,
                "normal_mach_area_weighted_mean": (
                    normal_mach_integral / area if evaluate_normal_mach else None
                ),
                "backflow_area_fraction": (
                    totals["backflow_area_m2"] / area
                    if evaluate_normal_mach
                    else None
                ),
                "subsonic_normal_area_fraction": (
                    totals["subsonic_normal_area_m2"] / area
                    if evaluate_normal_mach
                    else None
                ),
                "nonpositive_normal_mach_area_fraction": (
                    totals["nonpositive_normal_mach_area_m2"] / area
                    if evaluate_normal_mach
                    else None
                ),
                "reverse_to_forward_mass_flow_ratio": (
                    reverse / forward if forward > 0.0 else None
                ),
                "worst_normal_mach_faces": sorted(
                    normal_mach_faces,
                    key=lambda record: record["face_centroid_normal_mach"],
                )[:25],
            }
        )
        if not evaluate_normal_mach:
            for key in (
                "backflow_area_m2",
                "subsonic_normal_area_m2",
                "nonpositive_normal_mach_area_m2",
                "normal_mach_vertex_min",
                "normal_mach_vertex_max",
                "normal_mach_face_centroid_min",
                "normal_mach_face_centroid_max",
            ):
                totals[key] = None
        marker_results[marker] = totals

    if marker_keys != exterior_triangles:
        raise OutletDiagnosticError(
            "solver markers do not exactly cover all exterior volume faces"
        )
    global_net = sum(result["net_mass_flow_kg_s"] for result in marker_results.values())
    open_markers = (farfield_marker, *outlet_markers)
    open_inflow = sum(marker_results[name]["reverse_mass_flow_kg_s"] for name in open_markers)
    open_outflow = sum(marker_results[name]["forward_mass_flow_kg_s"] for name in open_markers)
    denominator = max(0.5 * (open_inflow + open_outflow), 1.0e-30)
    outlet_observations = [marker_results[name] for name in outlet_markers]
    sampled_supersonic = all(
        result["normal_mach_vertex_min"] > 1.0
        and result["backflow_area_fraction"] == 0.0
        for result in outlet_observations
    )
    return {
        "computation_status": "PASS",
        "boundary_decision": "NOT_FROZEN",
        "production_eligible": False,
        "observation": (
            "ALL_SAMPLED_OUTLET_VERTICES_SUPERSONIC_WITHOUT_BACKFLOW"
            if sampled_supersonic
            else "OUTLET_SUPERSONIC_OR_BACKFLOW_CRITERIA_NOT_MET"
        ),
        "mesh": {
            "path": str(mesh.path),
            "sha256": mesh.sha256,
            "point_count": len(mesh.points),
            "tetrahedron_count": len(mesh.tetrahedra),
            "prism_count": len(mesh.prisms),
            "volume_element_count": len(mesh.tetrahedra) + len(mesh.prisms),
            "marker_count": len(mesh.markers),
            "parser_scope": (
                "type-10 Tet4 and type-13 Prism6 volumes with type-5 "
                "triangle markers"
            ),
        },
        "coordinate_mapping": mapping_evidence,
        "array_checks": {
            "association": "point",
            "required": {
                "Density": 1,
                "Momentum": 3,
                "Velocity": 3,
                "Mach": 1,
            },
            "finite": True,
            "maximum_relative_momentum_consistency_error": momentum_consistency,
        },
        "topology": {
            "exterior_face_count": len(exterior_keys),
            "exterior_triangle_face_count": len(exterior_triangles),
            "exterior_quadrilateral_face_count": len(exterior_quadrilaterals),
            "marker_face_count": len(marker_keys),
            "coverage_complete": True,
            "owner_errors": 0,
            "degenerate_faces": 0,
        },
        "markers": marker_results,
        "outlet_markers": list(outlet_markers),
        "mass_balance": {
            "global_signed_mass_flow_kg_s": global_net,
            "open_boundary_inflow_kg_s": open_inflow,
            "open_boundary_outflow_kg_s": open_outflow,
            "wall_signed_mass_flow_kg_s": marker_results[wall_marker][
                "net_mass_flow_kg_s"
            ],
            "relative_global_imbalance": abs(global_net) / denominator,
            "sign_convention": "positive is outward from the fluid domain",
        },
        "limitations": [
            "diagnostic supports Tet4/Prism6 mixed meshes with triangular markers",
            "diagnostic output is not a production boundary-condition freeze",
            "no-slip wall normal Mach/backflow is undefined and intentionally omitted",
            "wall signed flux is reported only as a numerical consistency check",
        ],
    }
