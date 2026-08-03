"""Gmsh Python-API bridge and SU2 mesh validation utilities.

The controlling process imports :mod:`gmsh` directly and never opens the
Gmsh GUI.  The Gmsh executable is used only for a short ``--version`` probe;
all geometry creation and mesh writing are performed through the Python API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import traceback
from typing import Any


PathLike = str | os.PathLike[str]

DEFAULT_LENGTH_M = 1.0
DEFAULT_HEIGHT_M = 0.5
DEFAULT_MESH_SIZE_M = 0.025
MIN_SMOKE_ELEMENTS = 1_000
MAX_SMOKE_ELEMENTS = 5_000

_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_]+)\s*=\s*(.*?)\s*$")
_INTEGER_RE = re.compile(r"^[+-]?\d+(?=\s|$)")
_NONFINITE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:nan|[+-]?inf(?:inity)?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


class GmshBridgeError(RuntimeError):
    """Raised when Gmsh import, initialization, meshing, or export fails."""


class SU2MeshValidationError(ValueError):
    """Raised when a SU2 mesh text file fails structural validation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _csv_write(
    path: Path,
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Write a deterministic UTF-8 catalog suitable for Python and Excel."""

    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def _path_is_reparse(path: Path) -> bool:
    """Return whether an existing path is a symlink/junction/reparse point."""

    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _path_is_read_only(path: Path) -> bool:
    """Check read-only metadata without attempting to modify the input file."""

    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return bool(attributes & read_only_flag)
    writable_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable_bits)


def _existing_path_has_reparse_component(path: Path) -> bool:
    """Inspect every existing component before resolving a potentially linked path."""

    absolute = Path(os.path.abspath(path))
    components = [absolute, *absolute.parents]
    for component in reversed(components):
        if component.exists() and _path_is_reparse(component):
            return True
    return False


def _finite_vector(values: Any, *, length: int, label: str) -> list[float]:
    """Convert a Gmsh list/ndarray to finite built-in floats."""

    try:
        converted = [float(value) for value in values]
    except (TypeError, ValueError) as error:
        raise GmshBridgeError(f"{label} is not a numeric vector") from error
    if len(converted) != length:
        raise GmshBridgeError(
            f"{label} has {len(converted)} values; expected {length}"
        )
    if not all(math.isfinite(value) for value in converted):
        raise GmshBridgeError(f"{label} contains a non-finite value: {converted}")
    return converted


def _surface_normal_samples(gmsh: Any, tag: int) -> list[list[float]]:
    """Sample finite surface normals without creating a mesh.

    Candidate parameters are kept away from parameter-domain edges.  When the
    installed API exposes ``isInside`` it is used one point at a time so trimmed
    faces are not assigned a fabricated sample.  Normal direction is recorded as
    geometry evidence only; it is never interpreted as an outward fluid normal.
    """

    lower_raw, upper_raw = gmsh.model.getParametrizationBounds(2, tag)
    lower = _finite_vector(
        lower_raw, length=2, label=f"surface {tag} parametrization minimum"
    )
    upper = _finite_vector(
        upper_raw, length=2, label=f"surface {tag} parametrization maximum"
    )
    if any(maximum < minimum for minimum, maximum in zip(lower, upper, strict=True)):
        raise GmshBridgeError(
            f"surface {tag} has reversed parametrization bounds: {lower}, {upper}"
        )

    fractions = (0.2, 0.35, 0.5, 0.65, 0.8)
    candidates: list[list[float]] = []
    is_inside = getattr(gmsh.model, "isInside", None)
    for first_fraction in fractions:
        for second_fraction in fractions:
            parameters = [
                lower[0] + first_fraction * (upper[0] - lower[0]),
                lower[1] + second_fraction * (upper[1] - lower[1]),
            ]
            if is_inside is not None:
                try:
                    if int(is_inside(2, tag, parameters, parametric=True)) < 1:
                        continue
                except Exception as error:
                    raise GmshBridgeError(
                        f"surface {tag} inside test failed: {error}"
                    ) from error
            candidates.append(parameters)
            if len(candidates) == 5:
                break
        if len(candidates) == 5:
            break
    if not candidates:
        raise GmshBridgeError(
            f"surface {tag} has no valid interior parametrization sample"
        )

    flattened = [value for pair in candidates for value in pair]
    raw_normals = gmsh.model.getNormal(tag, flattened)
    normals = _finite_vector(
        raw_normals,
        length=3 * len(candidates),
        label=f"surface {tag} normal samples",
    )
    grouped = [normals[index : index + 3] for index in range(0, len(normals), 3)]
    for normal in grouped:
        norm = math.sqrt(sum(component * component for component in normal))
        if not math.isfinite(norm) or norm <= 0.0:
            raise GmshBridgeError(
                f"surface {tag} returned a zero or invalid normal: {normal}"
            )
    return grouped


def _module_candidate_path() -> str | None:
    try:
        specification = importlib.util.find_spec("gmsh")
    except (ImportError, AttributeError, ValueError):
        return None
    if specification is None or specification.origin is None:
        return None
    return str(Path(specification.origin).expanduser().resolve())


def _module_path(module: Any) -> str | None:
    value = getattr(module, "__file__", None)
    if value is None:
        return _module_candidate_path()
    return str(Path(value).expanduser().resolve())


def _import_gmsh() -> Any:
    # This direct, local import is intentional.  Importing cfdpipe itself must
    # remain possible on machines where the optional Gmsh SDK is not present.
    import gmsh

    return gmsh


def _parse_required_integer(value: str, key: str) -> int:
    uncommented = value.split("%", 1)[0].strip()
    match = _INTEGER_RE.match(uncommented)
    if match is None:
        raise SU2MeshValidationError(f"{key} is not an integer: {value!r}")
    trailing = uncommented[match.end() :].strip()
    if trailing:
        raise SU2MeshValidationError(
            f"{key} contains unexpected trailing text: {value!r}"
        )
    return int(match.group(0))


def _section_rows(
    lines: Sequence[str],
    assignment_line: int,
    count: int,
    section_name: str,
) -> list[tuple[int, str]]:
    """Return exactly ``count`` non-comment rows after an assignment line."""

    rows: list[tuple[int, str]] = []
    for line_number in range(assignment_line + 1, len(lines) + 1):
        candidate = lines[line_number - 1].split("%", 1)[0].strip()
        if not candidate:
            continue
        if _ASSIGNMENT_RE.match(candidate) is not None:
            raise SU2MeshValidationError(
                f"{section_name} ended after {len(rows)} rows; expected {count}"
            )
        rows.append((line_number, candidate))
        if len(rows) == count:
            return rows
    raise SU2MeshValidationError(
        f"{section_name} ended after {len(rows)} rows; expected {count}"
    )


def _parse_int_token(value: str, label: str, line_number: int) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise SU2MeshValidationError(
            f"{label} is not an integer at line {line_number}: {value!r}"
        ) from error


def _triangle_mesh_quality(
    lines: Sequence[str],
    *,
    nelem: int,
    nelem_line: int,
    npoin: int,
    npoin_line: int,
    farfield_count: int,
    farfield_elements_line: int,
) -> dict[str, Any]:
    """Independently validate 2D triangle geometry and edge topology."""

    element_rows = _section_rows(lines, nelem_line, nelem, "NELEM")
    point_rows = _section_rows(lines, npoin_line, npoin, "NPOIN")
    farfield_rows = _section_rows(
        lines,
        farfield_elements_line,
        farfield_count,
        "MARKER_ELEMS[farfield]",
    )

    points: dict[int, tuple[float, float]] = {}
    coordinate_owners: dict[tuple[float, float], int] = {}
    for ordinal, (line_number, row) in enumerate(point_rows):
        tokens = row.split()
        if len(tokens) not in (2, 3):
            raise SU2MeshValidationError(
                f"2D point row at line {line_number} must contain x, y, "
                "and optional point id"
            )
        try:
            x_coordinate = float(tokens[0])
            y_coordinate = float(tokens[1])
        except ValueError as error:
            raise SU2MeshValidationError(
                f"invalid point coordinate at line {line_number}: {row!r}"
            ) from error
        if not math.isfinite(x_coordinate) or not math.isfinite(y_coordinate):
            raise SU2MeshValidationError(
                f"non-finite point coordinate at line {line_number}"
            )
        point_id = (
            ordinal
            if len(tokens) == 2
            else _parse_int_token(tokens[2], "point id", line_number)
        )
        if point_id in points:
            raise SU2MeshValidationError(f"duplicate point id {point_id}")
        coordinate = (x_coordinate, y_coordinate)
        if coordinate in coordinate_owners:
            raise SU2MeshValidationError(
                "duplicate point coordinates for ids "
                f"{coordinate_owners[coordinate]} and {point_id}: {coordinate}"
            )
        points[point_id] = coordinate
        coordinate_owners[coordinate] = point_id

    triangles: list[tuple[int, int, int, int]] = []
    for line_number, row in element_rows:
        tokens = row.split()
        if len(tokens) not in (4, 5):
            raise SU2MeshValidationError(
                f"triangle row at line {line_number} has invalid field count"
            )
        element_type = _parse_int_token(tokens[0], "element type", line_number)
        if element_type != 5:
            raise SU2MeshValidationError(
                f"2D smoke element at line {line_number} is not an SU2 triangle: "
                f"type {element_type}"
            )
        node_ids = tuple(
            _parse_int_token(value, "triangle node id", line_number)
            for value in tokens[1:4]
        )
        if len(set(node_ids)) != 3:
            raise SU2MeshValidationError(
                f"triangle at line {line_number} repeats a node id"
            )
        missing_nodes = sorted(set(node_ids) - points.keys())
        if missing_nodes:
            raise SU2MeshValidationError(
                f"triangle at line {line_number} references missing nodes: "
                f"{missing_nodes}"
            )
        triangles.append((node_ids[0], node_ids[1], node_ids[2], line_number))

    x_values = [coordinates[0] for coordinates in points.values()]
    y_values = [coordinates[1] for coordinates in points.values()]
    coordinate_scale = max(
        max(x_values) - min(x_values),
        max(y_values) - min(y_values),
        1.0e-15,
    )
    area_tolerance = max(coordinate_scale * coordinate_scale * 1.0e-14, 1.0e-30)
    edge_tolerance = max(coordinate_scale * 1.0e-14, 1.0e-30)

    areas: list[float] = []
    normalized_qualities: list[float] = []
    aspect_ratios: list[float] = []
    edge_length_ratios: list[float] = []
    angles: list[float] = []
    edge_counts: dict[tuple[int, int], int] = {}
    negative_signed_area_count = 0
    degenerate_triangle_count = 0
    for first, second, third, line_number in triangles:
        point_a, point_b, point_c = points[first], points[second], points[third]
        signed_double_area = (
            (point_b[0] - point_a[0]) * (point_c[1] - point_a[1])
            - (point_b[1] - point_a[1]) * (point_c[0] - point_a[0])
        )
        area = abs(signed_double_area) * 0.5
        if area <= area_tolerance:
            degenerate_triangle_count += 1
            raise SU2MeshValidationError(
                f"degenerate triangle at line {line_number}: area={area}"
            )
        if signed_double_area < 0.0:
            negative_signed_area_count += 1

        side_a = math.dist(point_b, point_c)
        side_b = math.dist(point_a, point_c)
        side_c = math.dist(point_a, point_b)
        sides = (side_a, side_b, side_c)
        if min(sides) <= edge_tolerance or not all(math.isfinite(side) for side in sides):
            raise SU2MeshValidationError(
                f"invalid triangle edge length at line {line_number}"
            )
        side_square_sum = sum(side * side for side in sides)
        normalized_quality = 4.0 * math.sqrt(3.0) * area / side_square_sum
        longest_side = max(sides)
        aspect_ratio = longest_side * longest_side / (2.0 * area)
        triangle_angles: list[float] = []
        for opposite, adjacent_one, adjacent_two in (
            (side_a, side_b, side_c),
            (side_b, side_a, side_c),
            (side_c, side_a, side_b),
        ):
            cosine = (
                adjacent_one * adjacent_one
                + adjacent_two * adjacent_two
                - opposite * opposite
            ) / (2.0 * adjacent_one * adjacent_two)
            triangle_angles.append(
                math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
            )

        areas.append(area)
        normalized_qualities.append(normalized_quality)
        aspect_ratios.append(aspect_ratio)
        edge_length_ratios.append(longest_side / min(sides))
        angles.extend(triangle_angles)
        for edge in ((first, second), (second, third), (third, first)):
            canonical_edge = tuple(sorted(edge))
            edge_counts[canonical_edge] = edge_counts.get(canonical_edge, 0) + 1

    if negative_signed_area_count:
        raise SU2MeshValidationError(
            f"mesh contains {negative_signed_area_count} negatively oriented triangles"
        )
    nonmanifold_edges = [edge for edge, count in edge_counts.items() if count > 2]
    if nonmanifold_edges:
        raise SU2MeshValidationError(
            f"mesh contains {len(nonmanifold_edges)} non-manifold edges"
        )
    topology_boundary = {edge for edge, count in edge_counts.items() if count == 1}

    farfield_edges: list[tuple[int, int]] = []
    for line_number, row in farfield_rows:
        tokens = row.split()
        if len(tokens) not in (3, 4):
            raise SU2MeshValidationError(
                f"farfield edge row at line {line_number} has invalid field count"
            )
        element_type = _parse_int_token(tokens[0], "boundary element type", line_number)
        if element_type != 3:
            raise SU2MeshValidationError(
                f"farfield element at line {line_number} is not an SU2 line: "
                f"type {element_type}"
            )
        node_ids = (
            _parse_int_token(tokens[1], "farfield node id", line_number),
            _parse_int_token(tokens[2], "farfield node id", line_number),
        )
        if node_ids[0] == node_ids[1] or any(node not in points for node in node_ids):
            raise SU2MeshValidationError(
                f"invalid farfield edge at line {line_number}: {node_ids}"
            )
        farfield_edges.append(tuple(sorted(node_ids)))
    if len(set(farfield_edges)) != len(farfield_edges):
        raise SU2MeshValidationError("farfield contains duplicate boundary edges")
    if set(farfield_edges) != topology_boundary:
        missing = len(topology_boundary - set(farfield_edges))
        extra = len(set(farfield_edges) - topology_boundary)
        raise SU2MeshValidationError(
            "farfield does not exactly cover the triangle boundary: "
            f"missing={missing}, extra={extra}"
        )
    boundary_neighbors: dict[int, set[int]] = {}
    for first, second in topology_boundary:
        boundary_neighbors.setdefault(first, set()).add(second)
        boundary_neighbors.setdefault(second, set()).add(first)
    invalid_boundary_degrees = {
        node: len(neighbors)
        for node, neighbors in boundary_neighbors.items()
        if len(neighbors) != 2
    }
    if invalid_boundary_degrees:
        raise SU2MeshValidationError(
            f"farfield boundary is not a closed 2-regular loop: {invalid_boundary_degrees}"
        )
    unvisited = set(boundary_neighbors)
    boundary_component_count = 0
    while unvisited:
        boundary_component_count += 1
        stack = [unvisited.pop()]
        while stack:
            node = stack.pop()
            for neighbor in boundary_neighbors[node]:
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    stack.append(neighbor)
    if boundary_component_count != 1:
        raise SU2MeshValidationError(
            "farfield must form exactly one closed boundary component; got "
            f"{boundary_component_count}"
        )

    return {
        "status": "PASS",
        "element_type": "first_order_triangle",
        "triangle_count": len(triangles),
        "finite": True,
        "duplicate_coordinate_count": 0,
        "positive_signed_area_count": len(triangles),
        "negative_signed_area_count": negative_signed_area_count,
        "degenerate_triangle_count": degenerate_triangle_count,
        "minimum_area_m2": min(areas),
        "maximum_area_m2": max(areas),
        "total_area_m2": sum(areas),
        "minimum_normalized_quality": min(normalized_qualities),
        "maximum_aspect_ratio": max(aspect_ratios),
        "maximum_edge_length_ratio": max(edge_length_ratios),
        "minimum_angle_deg": min(angles),
        "maximum_angle_deg": max(angles),
        "boundary_edge_count": len(topology_boundary),
        "interior_edge_count": sum(1 for count in edge_counts.values() if count == 2),
        "nonmanifold_edge_count": len(nonmanifold_edges),
        "farfield_edge_count": len(farfield_edges),
        "boundary_component_count": boundary_component_count,
    }


def validate_su2_mesh(path: PathLike) -> dict[str, Any]:
    """Validate the required structure of a two-dimensional SU2 mesh.

    The validator deliberately operates on text only; it does not invoke SU2.
    A :class:`SU2MeshValidationError` makes the CLI return a non-zero status.
    """

    mesh_path = Path(path).expanduser().resolve()
    if not mesh_path.is_file():
        raise SU2MeshValidationError(f"SU2 mesh file does not exist: {mesh_path}")
    if mesh_path.stat().st_size <= 0:
        raise SU2MeshValidationError(f"SU2 mesh file is empty: {mesh_path}")

    try:
        text = mesh_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SU2MeshValidationError(
            f"SU2 mesh is not valid UTF-8 text: {mesh_path}"
        ) from error
    if not text.strip():
        raise SU2MeshValidationError(f"SU2 mesh file is empty: {mesh_path}")
    nonfinite = _NONFINITE_RE.search(text)
    if nonfinite is not None:
        raise SU2MeshValidationError(
            f"SU2 mesh contains non-finite token {nonfinite.group(0)!r}"
        )

    lines = text.splitlines()
    assignments: list[tuple[str, str, int]] = []
    for line_number, line in enumerate(lines, start=1):
        candidate = line.split("%", 1)[0]
        match = _ASSIGNMENT_RE.match(candidate)
        if match is not None:
            assignments.append(
                (match.group(1).upper(), match.group(2).strip(), line_number)
            )

    global_counts: dict[str, int] = {}
    global_count_lines: dict[str, int] = {}
    for key in ("NDIME", "NELEM", "NPOIN", "NMARK"):
        values = [(value, line) for name, value, line in assignments if name == key]
        if len(values) != 1:
            raise SU2MeshValidationError(
                f"expected exactly one {key} assignment, found {len(values)}"
            )
        global_counts[key] = _parse_required_integer(values[0][0], key)
        global_count_lines[key] = values[0][1]

    if global_counts["NDIME"] != 2:
        raise SU2MeshValidationError(
            f"NDIME must be 2, got {global_counts['NDIME']}"
        )
    for key in ("NELEM", "NPOIN", "NMARK"):
        if global_counts[key] <= 0:
            raise SU2MeshValidationError(f"{key} must be greater than zero")

    markers: dict[str, int] = {}
    marker_elements_lines: dict[str, int] = {}
    pending_marker: str | None = None
    for key, value, line_number in assignments:
        if key == "MARKER_TAG":
            if pending_marker is not None:
                raise SU2MeshValidationError(
                    f"MARKER_TAG {pending_marker!r} has no MARKER_ELEMS"
                )
            pending_marker = value.strip()
            if not pending_marker:
                raise SU2MeshValidationError(
                    f"empty MARKER_TAG at line {line_number}"
                )
        elif key == "MARKER_ELEMS":
            if pending_marker is None:
                raise SU2MeshValidationError(
                    f"MARKER_ELEMS at line {line_number} has no MARKER_TAG"
                )
            if pending_marker in markers:
                raise SU2MeshValidationError(
                    f"duplicate MARKER_TAG {pending_marker!r}"
                )
            markers[pending_marker] = _parse_required_integer(
                value, f"MARKER_ELEMS[{pending_marker}]"
            )
            marker_elements_lines[pending_marker] = line_number
            pending_marker = None
    if pending_marker is not None:
        raise SU2MeshValidationError(
            f"MARKER_TAG {pending_marker!r} has no MARKER_ELEMS"
        )
    if len(markers) != global_counts["NMARK"]:
        raise SU2MeshValidationError(
            "NMARK does not match the number of marker sections: "
            f"{global_counts['NMARK']} != {len(markers)}"
        )
    if "farfield" not in markers:
        raise SU2MeshValidationError("MARKER_TAG= farfield is missing")
    if markers["farfield"] <= 0:
        raise SU2MeshValidationError(
            "farfield MARKER_ELEMS must be greater than zero"
        )

    quality = _triangle_mesh_quality(
        lines,
        nelem=global_counts["NELEM"],
        nelem_line=global_count_lines["NELEM"],
        npoin=global_counts["NPOIN"],
        npoin_line=global_count_lines["NPOIN"],
        farfield_count=markers["farfield"],
        farfield_elements_line=marker_elements_lines["farfield"],
    )

    return {
        "path": str(mesh_path),
        "size_bytes": mesh_path.stat().st_size,
        "ndime": global_counts["NDIME"],
        "nelem": global_counts["NELEM"],
        "npoin": global_counts["NPOIN"],
        "nmark": global_counts["NMARK"],
        "markers": markers,
        "quality": quality,
    }


class GmshBridge:
    """Connect ordinary Python to Gmsh's Python API without a GUI."""

    def __init__(self, runner: Any | None = None, toolchain: Any | None = None) -> None:
        self.runner = runner
        self.toolchain = toolchain

    @staticmethod
    def _version(module: Any) -> str:
        value = getattr(module, "__version__", None)
        if value is not None:
            return str(value)
        value = getattr(module, "GMSH_API_VERSION", None)
        if value is not None:
            return str(value)
        return "unknown"

    def _probe_cli(self, output_directory: Path, timeout: float | None) -> dict[str, Any]:
        if self.runner is None or self.toolchain is None:
            raise GmshBridgeError(
                "Gmsh CLI provenance requires both CommandRunner and Toolchain"
            )
        resolved = self.toolchain.resolve("gmsh")
        result = self.runner.run(
            resolved.path,
            args=("--version",),
            cwd=output_directory,
            timeout=timeout,
            check=True,
            live_output=False,
            dry_run=False,
            name="gmsh-cli-version",
        )
        combined = (result.stdout or result.stderr).strip()
        match = re.search(r"\d+(?:\.\d+)+", combined)
        if match is None:
            raise GmshBridgeError(
                f"could not parse Gmsh CLI version from output: {combined!r}"
            )
        return {
            "path": str(Path(resolved.path).expanduser().resolve()),
            "source": str(resolved.source),
            "version": match.group(0),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": result.as_metadata(),
        }

    @staticmethod
    def _physical_groups(gmsh: Any) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for dimension, physical_tag in gmsh.model.getPhysicalGroups():
            name = gmsh.model.getPhysicalName(dimension, physical_tag)
            entities = gmsh.model.getEntitiesForPhysicalGroup(
                dimension, physical_tag
            )
            groups.append(
                {
                    "name": str(name),
                    "dimension": int(dimension),
                    "physical_tag": int(physical_tag),
                    "entity_tags": [int(tag) for tag in entities],
                }
            )
        return groups

    @staticmethod
    def _mesh_counts(gmsh: Any) -> tuple[int, int]:
        node_tags = gmsh.model.mesh.getNodes()[0]
        element_types, element_tags, _ = gmsh.model.mesh.getElements(2)
        element_count = 0
        for element_type, tags in zip(element_types, element_tags, strict=True):
            properties = gmsh.model.mesh.getElementProperties(element_type)
            element_name = str(properties[0])
            element_dimension = int(properties[1])
            element_order = int(properties[2])
            if (
                element_dimension != 2
                or not element_name.lower().startswith("triangle")
                or element_order != 1
            ):
                raise GmshBridgeError(
                    "smoke mesh must contain only first-order triangles; got "
                    f"{element_name!r}, dimension={element_dimension}, "
                    f"order={element_order}"
                )
            element_count += len(tags)
        return len(node_tags), element_count

    @staticmethod
    def _persist(
        output_directory: Path,
        manifest: dict[str, Any],
        log_lines: Sequence[str],
    ) -> None:
        log_path = output_directory / "gmsh.log"
        manifest_path = output_directory / "gmsh_manifest.json"
        rendered_log = "\n".join(str(line) for line in log_lines).rstrip() + "\n"
        log_path.write_text(rendered_log, encoding="utf-8")
        _json_write(manifest_path, manifest)

    def smoke(
        self,
        output_directory: PathLike | None = None,
        *,
        timeout: float | None = 10.0,
    ) -> dict[str, Any]:
        """Run either a lifecycle probe or the complete smoke mesh."""

        if output_directory is not None:
            return self.build_smoke_mesh(output_directory, timeout=timeout)

        gmsh = _import_gmsh()
        gmsh.initialize()
        try:
            return {
                "module": "gmsh",
                "version": self._version(gmsh),
                "python_module_path": _module_path(gmsh),
            }
        finally:
            gmsh.finalize()

    def build_smoke_mesh(
        self,
        output_directory: PathLike,
        *,
        length_m: float = DEFAULT_LENGTH_M,
        height_m: float = DEFAULT_HEIGHT_M,
        mesh_size_m: float = DEFAULT_MESH_SIZE_M,
        timeout: float | None = 10.0,
    ) -> dict[str, Any]:
        """Generate and validate the small rectangular connection mesh."""

        for name, value in (
            ("length_m", length_m),
            ("height_m", height_m),
            ("mesh_size_m", mesh_size_m),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a real number")
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise ValueError(f"{name} must be finite and greater than zero")

        output_dir = Path(output_directory).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        msh_path = output_dir / "smoke.msh"
        su2_path = output_dir / "smoke.su2"
        started_at = _utc_now()
        inputs = {
            "length_m": float(length_m),
            "height_m": float(height_m),
            "mesh_size_m": float(mesh_size_m),
            "element_type": "first_order_triangle",
            "element_count_range": [MIN_SMOKE_ELEMENTS, MAX_SMOKE_ELEMENTS],
            "output_directory": str(output_dir),
        }
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "started_at": started_at,
            "ended_at": None,
            "gmsh_version": None,
            "python_module_path": _module_candidate_path(),
            "gmsh_cli_path": None,
            "gmsh_cli_source": None,
            "gmsh_cli_version": None,
            "gmsh_cli_command": None,
            "inputs": inputs,
            "node_count": 0,
            "element_count": 0,
            "markers": [],
            "output_sha256": {},
            "su2_validation": None,
            "error": None,
        }
        log_lines = [
            f"started_at: {started_at}",
            f"inputs: {json.dumps(inputs, ensure_ascii=False, sort_keys=True)}",
        ]

        try:
            cli = self._probe_cli(output_dir, timeout)
            manifest["gmsh_cli_path"] = cli["path"]
            manifest["gmsh_cli_source"] = cli["source"]
            manifest["gmsh_cli_version"] = cli["version"]
            manifest["gmsh_cli_command"] = cli["command"]
            log_lines.extend(
                (
                    f"gmsh_cli_path: {cli['path']}",
                    f"gmsh_cli_source: {cli['source']}",
                    f"gmsh_cli_version: {cli['version']}",
                    f"gmsh_cli_stdout: {cli['stdout'].rstrip()}",
                    f"gmsh_cli_stderr: {cli['stderr'].rstrip()}",
                )
            )
        except BaseException as error:
            cli_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": cli_traceback,
            }
            log_lines.extend(("Gmsh CLI probe failed:", cli_traceback, "status: FAIL"))
            self._persist(output_dir, manifest, log_lines)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise GmshBridgeError(
                f"Gmsh CLI version probe failed: {error}"
            ) from error

        try:
            gmsh = _import_gmsh()
        except BaseException as error:
            failure_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": failure_traceback,
            }
            log_lines.extend(
                (
                    f"python_module_path: {manifest['python_module_path']}",
                    failure_traceback,
                )
            )
            self._persist(output_dir, manifest, log_lines)
            raise GmshBridgeError(f"could not import gmsh: {error}") from error

        manifest["gmsh_version"] = self._version(gmsh)
        manifest["python_module_path"] = _module_path(gmsh)
        log_lines.extend(
            (
                f"gmsh_version: {manifest['gmsh_version']}",
                f"python_module_path: {manifest['python_module_path']}",
            )
        )

        try:
            gmsh.initialize()
        except BaseException as error:
            failure_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": failure_traceback,
            }
            log_lines.append(failure_traceback)
            self._persist(output_dir, manifest, log_lines)
            raise GmshBridgeError(f"Gmsh initialization failed: {error}") from error

        api_logger_started = False
        operation_error: BaseException | None = None
        operation_traceback: str | None = None
        try:
            logger = getattr(gmsh, "logger", None)
            if logger is not None:
                logger.start()
                api_logger_started = True

            gmsh.model.add("cfdpipe_connection_smoke")
            surface_tag = gmsh.model.occ.addRectangle(
                0.0, 0.0, 0.0, float(length_m), float(height_m)
            )
            gmsh.model.occ.synchronize()
            boundary = gmsh.model.getBoundary(
                [(2, surface_tag)], oriented=False, recursive=False
            )
            boundary_tags = [int(tag) for dimension, tag in boundary if dimension == 1]
            if len(boundary_tags) != 4:
                raise GmshBridgeError(
                    f"expected four rectangle boundary curves, got {boundary_tags}"
                )

            farfield_tag = gmsh.model.addPhysicalGroup(1, boundary_tags)
            gmsh.model.setPhysicalName(1, farfield_tag, "farfield")
            fluid_tag = gmsh.model.addPhysicalGroup(2, [surface_tag])
            gmsh.model.setPhysicalName(2, fluid_tag, "fluid")

            points = gmsh.model.getEntities(0)
            gmsh.model.mesh.setSize(points, float(mesh_size_m))
            gmsh.option.setNumber("Mesh.MeshSizeMin", float(mesh_size_m))
            gmsh.option.setNumber("Mesh.MeshSizeMax", float(mesh_size_m))
            gmsh.option.setNumber("Mesh.Algorithm", 6)
            gmsh.option.setNumber("Mesh.RecombineAll", 0)
            gmsh.option.setNumber("Mesh.ElementOrder", 1)
            gmsh.model.mesh.generate(2)

            node_count, element_count = self._mesh_counts(gmsh)
            if not MIN_SMOKE_ELEMENTS <= element_count <= MAX_SMOKE_ELEMENTS:
                raise GmshBridgeError(
                    "smoke triangle count is outside the required range: "
                    f"{element_count} not in "
                    f"[{MIN_SMOKE_ELEMENTS}, {MAX_SMOKE_ELEMENTS}]"
                )
            manifest["node_count"] = node_count
            manifest["element_count"] = element_count
            manifest["markers"] = self._physical_groups(gmsh)
            marker_names = {entry["name"] for entry in manifest["markers"]}
            if not {"farfield", "fluid"}.issubset(marker_names):
                raise GmshBridgeError(
                    f"required physical names are missing: {sorted(marker_names)}"
                )

            gmsh.write(str(msh_path))
            gmsh.write(str(su2_path))
            validation = validate_su2_mesh(su2_path)
            manifest["su2_validation"] = validation
            manifest["output_sha256"] = {
                msh_path.name: _sha256(msh_path),
                su2_path.name: _sha256(su2_path),
            }
        except BaseException as error:
            operation_error = error
            operation_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
        finally:
            if api_logger_started:
                try:
                    log_lines.extend(str(message) for message in gmsh.logger.get())
                except BaseException as logger_error:
                    log_lines.append(f"gmsh.logger.get failed: {logger_error}")
                try:
                    gmsh.logger.stop()
                except BaseException as logger_error:
                    log_lines.append(f"gmsh.logger.stop failed: {logger_error}")
            try:
                gmsh.finalize()
            except BaseException as finalize_error:
                finalize_traceback = "".join(
                    traceback.format_exception(
                        type(finalize_error),
                        finalize_error,
                        finalize_error.__traceback__,
                    )
                )
                log_lines.append(finalize_traceback)
                if operation_error is None:
                    operation_error = finalize_error
                    operation_traceback = finalize_traceback

        manifest["ended_at"] = _utc_now()
        if operation_error is None:
            manifest["status"] = "PASS"
            log_lines.append("status: PASS")
        else:
            manifest["status"] = "FAIL"
            manifest["error"] = {
                "type": type(operation_error).__name__,
                "message": str(operation_error),
                "traceback": operation_traceback,
            }
            if operation_traceback is not None:
                log_lines.append(operation_traceback)
            log_lines.append("status: FAIL")
        self._persist(output_dir, manifest, log_lines)

        if operation_error is not None:
            if isinstance(operation_error, (KeyboardInterrupt, SystemExit)):
                raise operation_error
            raise GmshBridgeError(str(operation_error)) from operation_error
        return manifest

    def inspect_step_geometry(
        self,
        step_path: PathLike,
        output_directory: PathLike,
        *,
        allowed_output_root: PathLike | None = None,
    ) -> dict[str, Any]:
        """Import a read-only STEP and write geometry catalogs without meshing.

        This method is deliberately separate from :meth:`build_step_mesh`.  A
        successful result proves only that the STEP was imported and cataloged;
        every boundary role remains unclassified until an external marker
        contract is reviewed.  It never creates physical groups, generates a
        mesh, calls ``gmsh.write`` or launches an executable.
        """

        source_candidate = Path(step_path).expanduser()
        if not source_candidate.exists():
            raise ValueError(f"STEP path does not exist: {source_candidate}")
        if _existing_path_has_reparse_component(source_candidate):
            raise ValueError(
                f"STEP inspection refuses symlink/junction/reparse input: "
                f"{source_candidate}"
            )
        source = source_candidate.resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"STEP path is not a file: {source}")
        if source.suffix.lower() not in {".step", ".stp"}:
            raise ValueError(f"STEP path must end in .step or .stp: {source}")
        if not _path_is_read_only(source):
            raise ValueError(f"STEP source must be read-only before inspection: {source}")

        output_candidate = Path(output_directory).expanduser()
        output_dir = output_candidate.resolve()
        if allowed_output_root is not None:
            allowed_root = Path(allowed_output_root).expanduser().resolve()
            if output_dir != allowed_root and allowed_root not in output_dir.parents:
                raise ValueError(
                    f"geometry inspection output escapes {allowed_root}: {output_dir}"
                )
        if _existing_path_has_reparse_component(output_candidate):
            raise ValueError(
                "geometry inspection output contains a symlink/junction/reparse "
                f"component: {output_candidate}"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        if _path_is_reparse(output_dir):
            raise ValueError(
                f"geometry inspection output is a reparse point: {output_dir}"
            )

        surface_catalog_path = output_dir / "surface_catalog.csv"
        volume_catalog_path = output_dir / "volume_catalog.csv"
        candidates_path = output_dir / "marker_candidates.json"
        manifest_path = output_dir / "geometry_manifest.json"
        log_path = output_dir / "gmsh_geometry.log"

        started_at = _utc_now()
        source_hash_before = _sha256(source)
        source_size_before = source.stat().st_size
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "catalog_status": "NOT_COMPLETED",
            "classification_status": "UNCLASSIFIED",
            "marker_configuration_ready": False,
            "next_stage_allowed": False,
            "started_at": started_at,
            "ended_at": None,
            "source_step_path": str(source),
            "source_size_bytes": source_size_before,
            "source_read_only": True,
            "source_sha256_before": source_hash_before,
            "source_sha256_after": None,
            "source_unchanged": None,
            "target_geometry_unit": "M",
            "gmsh_version": None,
            "gmsh_module_path": _module_candidate_path(),
            "gmsh_cli_invoked": False,
            "import_options": {
                "api": "gmsh.model.occ.importShapes",
                "highest_dim_only": True,
                "format": "auto_from_filename",
            },
            "imported_entities": [],
            "entity_counts": {},
            "surface_count": 0,
            "volume_count": 0,
            "model_bounds_m": None,
            "outputs": {},
            "secondary_errors": [],
            "error": None,
        }
        log_lines = [
            f"started_at: {started_at}",
            f"source_step_path: {source}",
            f"source_sha256_before: {source_hash_before}",
            "target_geometry_unit: M",
            "operation: read-only STEP geometry catalog; no mesh or physical groups",
        ]

        gmsh: Any | None = None
        initialized = False
        initialization_attempted = False
        logger_started = False
        operation_error: BaseException | None = None
        operation_traceback: str | None = None
        surface_rows: list[dict[str, Any]] = []
        volume_rows: list[dict[str, Any]] = []
        candidate_payload: dict[str, Any] | None = None

        def record_primary(error: BaseException) -> None:
            nonlocal operation_error, operation_traceback
            rendered = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            if operation_error is None:
                operation_error = error
                operation_traceback = rendered
            else:
                manifest["secondary_errors"].append(
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": rendered,
                    }
                )
            log_lines.append(rendered)

        try:
            gmsh = _import_gmsh()
            manifest["gmsh_version"] = self._version(gmsh)
            manifest["gmsh_module_path"] = _module_path(gmsh)
            log_lines.extend(
                (
                    f"gmsh_version: {manifest['gmsh_version']}",
                    f"gmsh_module_path: {manifest['gmsh_module_path']}",
                )
            )

            is_initialized = getattr(gmsh, "isInitialized", None)
            if callable(is_initialized) and bool(is_initialized()):
                raise GmshBridgeError(
                    "geometry inspection requires a fresh Gmsh session owned by "
                    "cfdpipe"
                )

            initialization_attempted = True
            gmsh.initialize()
            initialized = True

            logger = getattr(gmsh, "logger", None)
            if logger is not None:
                logger.start()
                logger_started = True

            gmsh.model.add("cfdpipe_geometry_inspection")
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            imported_entities_raw = gmsh.model.occ.importShapes(str(source))
            gmsh.model.occ.synchronize()
            imported_entities = sorted(
                (
                    [int(dimension), int(tag)]
                    for dimension, tag in imported_entities_raw
                ),
                key=lambda item: (item[0], item[1]),
            )
            if not imported_entities:
                raise GmshBridgeError(f"Gmsh imported no entities from {source}")
            manifest["imported_entities"] = imported_entities

            entities_by_dimension: dict[int, list[int]] = {}
            for dimension in range(4):
                entities_by_dimension[dimension] = sorted(
                    int(tag)
                    for _, tag in gmsh.model.getEntities(dimension)
                )
            manifest["entity_counts"] = {
                str(dimension): len(tags)
                for dimension, tags in entities_by_dimension.items()
            }

            surface_tags = entities_by_dimension[2]
            volume_tags = entities_by_dimension[3]
            if not surface_tags:
                raise GmshBridgeError(
                    f"Gmsh imported no surfaces to catalog from {source}"
                )
            manifest["surface_count"] = len(surface_tags)
            manifest["volume_count"] = len(volume_tags)

            surface_records: list[dict[str, Any]] = []
            all_entity_bounds: list[list[float]] = []
            for tag in surface_tags:
                step_name = str(gmsh.model.getEntityName(2, tag))
                area = float(gmsh.model.occ.getMass(2, tag))
                if not math.isfinite(area) or area <= 0.0:
                    raise GmshBridgeError(
                        f"surface {tag} has invalid area from OCC: {area}"
                    )
                centroid = _finite_vector(
                    gmsh.model.occ.getCenterOfMass(2, tag),
                    length=3,
                    label=f"surface {tag} centroid",
                )
                bounding_box = _finite_vector(
                    gmsh.model.occ.getBoundingBox(2, tag),
                    length=6,
                    label=f"surface {tag} bounding box",
                )
                adjacent_raw, boundary_raw = gmsh.model.getAdjacencies(2, tag)
                adjacent_volumes = sorted(int(value) for value in adjacent_raw)
                boundary_curves = sorted(int(value) for value in boundary_raw)
                normal_samples = _surface_normal_samples(gmsh, tag)
                if len(adjacent_volumes) == 0:
                    topology_class = "orphan_surface_candidate"
                elif len(adjacent_volumes) == 1:
                    topology_class = "single_volume_boundary_candidate"
                elif len(adjacent_volumes) == 2:
                    topology_class = "two_volume_interface_candidate"
                else:
                    topology_class = "multi_volume_interface_candidate"

                record = {
                    "entity_tag": tag,
                    "step_name": step_name,
                    "area_m2": area,
                    "centroid_m": centroid,
                    "bounding_box_m": bounding_box,
                    "normal_samples": normal_samples,
                    "adjacent_volumes": adjacent_volumes,
                    "boundary_curves": boundary_curves,
                    "topology_class": topology_class,
                    "inferred_boundary_role": "unclassified",
                    "recognition_confidence": 0.0,
                    "classification_basis": (
                        "no externally confirmed marker contract; topology and "
                        "geometry are evidence only"
                    ),
                }
                surface_records.append(record)
                all_entity_bounds.append(bounding_box)
                surface_rows.append(
                    {
                        **record,
                        "centroid_m": json.dumps(centroid, separators=(",", ":")),
                        "bounding_box_m": json.dumps(
                            bounding_box, separators=(",", ":")
                        ),
                        "normal_samples": json.dumps(
                            normal_samples, separators=(",", ":")
                        ),
                        "adjacent_volumes": json.dumps(
                            adjacent_volumes, separators=(",", ":")
                        ),
                        "boundary_curves": json.dumps(
                            boundary_curves, separators=(",", ":")
                        ),
                    }
                )

            for tag in volume_tags:
                step_name = str(gmsh.model.getEntityName(3, tag))
                volume = float(gmsh.model.occ.getMass(3, tag))
                if not math.isfinite(volume) or volume <= 0.0:
                    raise GmshBridgeError(
                        f"volume {tag} has invalid volume from OCC: {volume}"
                    )
                centroid = _finite_vector(
                    gmsh.model.occ.getCenterOfMass(3, tag),
                    length=3,
                    label=f"volume {tag} centroid",
                )
                bounding_box = _finite_vector(
                    gmsh.model.occ.getBoundingBox(3, tag),
                    length=6,
                    label=f"volume {tag} bounding box",
                )
                _, surface_raw = gmsh.model.getAdjacencies(3, tag)
                boundary_surfaces = sorted(int(value) for value in surface_raw)
                if not boundary_surfaces:
                    boundary_surfaces = sorted(
                        record["entity_tag"]
                        for record in surface_records
                        if tag in record["adjacent_volumes"]
                    )
                volume_rows.append(
                    {
                        "entity_tag": tag,
                        "step_name": step_name,
                        "volume_m3": volume,
                        "centroid_m": json.dumps(centroid, separators=(",", ":")),
                        "bounding_box_m": json.dumps(
                            bounding_box, separators=(",", ":")
                        ),
                        "boundary_surfaces": json.dumps(
                            boundary_surfaces, separators=(",", ":")
                        ),
                    }
                )
                all_entity_bounds.append(bounding_box)

            model_bounds = [
                min(bounds[index] for bounds in all_entity_bounds)
                for index in range(3)
            ] + [
                max(bounds[index] for bounds in all_entity_bounds)
                for index in range(3, 6)
            ]
            manifest["model_bounds_m"] = model_bounds
            spans = [model_bounds[index + 3] - model_bounds[index] for index in range(3)]
            tolerance = max(max(spans, default=0.0) * 1.0e-8, 1.0e-12)
            axis_extrema: dict[str, list[int]] = {}
            for axis, index in zip(("x", "y", "z"), range(3), strict=True):
                minimum = model_bounds[index]
                maximum = model_bounds[index + 3]
                axis_extrema[f"{axis}_minimum_planar_candidates"] = [
                    int(record["entity_tag"])
                    for record in surface_records
                    if abs(record["bounding_box_m"][index] - minimum) <= tolerance
                    and abs(record["bounding_box_m"][index + 3] - minimum)
                    <= tolerance
                ]
                axis_extrema[f"{axis}_maximum_planar_candidates"] = [
                    int(record["entity_tag"])
                    for record in surface_records
                    if abs(record["bounding_box_m"][index] - maximum) <= tolerance
                    and abs(record["bounding_box_m"][index + 3] - maximum)
                    <= tolerance
                ]

            topology_groups: dict[str, list[int]] = {}
            name_groups: dict[str, list[int]] = {}
            unnamed_surfaces: list[int] = []
            for record in surface_records:
                topology_groups.setdefault(record["topology_class"], []).append(
                    int(record["entity_tag"])
                )
                if record["step_name"]:
                    name_groups.setdefault(str(record["step_name"]), []).append(
                        int(record["entity_tag"])
                    )
                else:
                    unnamed_surfaces.append(int(record["entity_tag"]))
            candidate_payload = {
                "status": "UNCLASSIFIED",
                "automatic_marker_mapping_created": False,
                "marker_configuration_ready": False,
                "next_stage_allowed": False,
                "reason": (
                    "geometry evidence cannot uniquely establish CFD boundary roles "
                    "without an externally reviewed marker contract"
                ),
                "surfaces": [
                    {
                        "entity_tag": int(record["entity_tag"]),
                        "step_name": str(record["step_name"]),
                        "area_m2": float(record["area_m2"]),
                        "centroid_m": list(record["centroid_m"]),
                        "bounding_box_m": list(record["bounding_box_m"]),
                        "normal_samples": [
                            list(sample) for sample in record["normal_samples"]
                        ],
                        "adjacent_volumes": list(record["adjacent_volumes"]),
                        "topology_class": str(record["topology_class"]),
                        "inferred_boundary_role": "unclassified",
                        "recognition_confidence": 0.0,
                    }
                    for record in surface_records
                ],
                "topology_groups": {
                    name: sorted(tags) for name, tags in sorted(topology_groups.items())
                },
                "step_name_groups": {
                    name: sorted(tags) for name, tags in sorted(name_groups.items())
                },
                "unnamed_surface_tags": sorted(unnamed_surfaces),
                "axis_extrema_candidates": axis_extrema,
                "review_requirements": [
                    "confirm each physical name against STEP name and geometry features",
                    "confirm farfield, wall, both rear outlets and measurement surface",
                    "do not treat the internal measurement surface as a solver boundary",
                    "write config/markers.toml only after ambiguity is resolved",
                ],
            }
        except BaseException as error:
            record_primary(error)
        finally:
            if gmsh is not None and logger_started:
                try:
                    log_lines.extend(str(message) for message in gmsh.logger.get())
                except BaseException as error:
                    record_primary(error)
                try:
                    gmsh.logger.stop()
                except BaseException as error:
                    record_primary(error)
            if gmsh is not None and initialized:
                try:
                    gmsh.finalize()
                except BaseException as error:
                    record_primary(error)
            elif gmsh is not None and initialization_attempted:
                is_initialized = getattr(gmsh, "isInitialized", None)
                if callable(is_initialized):
                    try:
                        if bool(is_initialized()):
                            gmsh.finalize()
                    except BaseException as error:
                        record_primary(error)

        try:
            source_hash_after = _sha256(source)
            source_size_after = source.stat().st_size
            source_read_only_after = _path_is_read_only(source)
            manifest["source_sha256_after"] = source_hash_after
            manifest["source_unchanged"] = (
                source_hash_after == source_hash_before
                and source_size_after == source_size_before
                and source_read_only_after
            )
            if not manifest["source_unchanged"]:
                raise GmshBridgeError(
                    "source STEP content, size, or read-only state changed during "
                    f"inspection: {source}"
                )
        except BaseException as error:
            record_primary(error)

        if operation_error is None:
            try:
                surface_fields = (
                    "entity_tag",
                    "step_name",
                    "area_m2",
                    "centroid_m",
                    "bounding_box_m",
                    "normal_samples",
                    "adjacent_volumes",
                    "boundary_curves",
                    "topology_class",
                    "inferred_boundary_role",
                    "recognition_confidence",
                    "classification_basis",
                )
                volume_fields = (
                    "entity_tag",
                    "step_name",
                    "volume_m3",
                    "centroid_m",
                    "bounding_box_m",
                    "boundary_surfaces",
                )
                _csv_write(surface_catalog_path, surface_fields, surface_rows)
                _csv_write(volume_catalog_path, volume_fields, volume_rows)
                if candidate_payload is None:
                    raise GmshBridgeError("marker candidate payload was not created")
                _json_write(candidates_path, candidate_payload)
                manifest["catalog_status"] = "PASS"
                manifest["status"] = "PASS"
                manifest["ended_at"] = _utc_now()
                log_lines.extend(
                    (
                        f"surface_count: {manifest['surface_count']}",
                        f"volume_count: {manifest['volume_count']}",
                        "classification_status: UNCLASSIFIED",
                        "marker_configuration_ready: false",
                        "next_stage_allowed: false",
                        "status: PASS",
                    )
                )
                log_path.write_text(
                    "\n".join(str(line) for line in log_lines).rstrip() + "\n",
                    encoding="utf-8",
                )
                for path in (
                    surface_catalog_path,
                    volume_catalog_path,
                    candidates_path,
                    log_path,
                ):
                    manifest["outputs"][path.name] = {
                        "path": str(path.resolve()),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
            except BaseException as error:
                record_primary(error)

        if operation_error is not None:
            manifest["status"] = "FAIL"
            manifest["catalog_status"] = "FAIL"
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(operation_error).__name__,
                "message": str(operation_error),
                "traceback": operation_traceback,
            }
            log_lines.append("status: FAIL")
            log_path.write_text(
                "\n".join(str(line) for line in log_lines).rstrip() + "\n",
                encoding="utf-8",
            )

        _json_write(manifest_path, manifest)
        if operation_error is not None:
            if isinstance(operation_error, (KeyboardInterrupt, SystemExit)):
                raise operation_error
            raise GmshBridgeError(str(operation_error)) from operation_error
        return manifest

    def build_step_mesh(
        self,
        step_path: PathLike,
        output_directory: PathLike,
        marker_mapping: Mapping[str, Mapping[str, Any]],
        mesh_options: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Import a configured STEP model and write MSH/SU2 through Gmsh API.

        ``marker_mapping`` must have the external form
        ``{name: {"dimension": int, "entity_tags": [int, ...]}}``.  No
        production entity tag or boundary role is embedded in this module.
        This entry point is implemented for a later authorized geometry stage;
        the current smoke workflow never calls it with the production STEP.
        """

        source = Path(step_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"STEP path is not a file: {source}")
        if not isinstance(marker_mapping, Mapping) or not marker_mapping:
            raise ValueError("marker_mapping must be a non-empty external mapping")
        if not isinstance(mesh_options, Mapping):
            raise TypeError("mesh_options must be a mapping")

        normalized_markers: list[tuple[str, int, list[int]]] = []
        normalized_names: set[str] = set()
        assigned_entities: dict[tuple[int, int], str] = {}
        for name, specification in marker_mapping.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("every physical group must have a non-empty name")
            normalized_name = name.strip()
            if normalized_name in normalized_names:
                raise ValueError(
                    f"duplicate physical group name after trimming: {normalized_name!r}"
                )
            normalized_names.add(normalized_name)
            if not isinstance(specification, Mapping):
                raise TypeError(f"marker {name!r} must be a mapping")
            dimension = specification.get("dimension")
            entity_tags = specification.get("entity_tags")
            if (
                isinstance(dimension, bool)
                or not isinstance(dimension, int)
                or dimension not in (0, 1, 2, 3)
            ):
                raise ValueError(f"marker {name!r} has invalid dimension")
            if (
                not isinstance(entity_tags, Sequence)
                or isinstance(entity_tags, (str, bytes))
                or not entity_tags
            ):
                raise ValueError(f"marker {name!r} needs entity_tags")
            tags: list[int] = []
            for tag in entity_tags:
                if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
                    raise ValueError(f"marker {name!r} has invalid entity tag {tag!r}")
                entity_key = (dimension, tag)
                previous_name = assigned_entities.get(entity_key)
                if previous_name is not None:
                    raise ValueError(
                        f"entity ({dimension}, {tag}) is assigned to both "
                        f"{previous_name!r} and {normalized_name!r}"
                    )
                assigned_entities[entity_key] = normalized_name
                tags.append(tag)
            normalized_markers.append((normalized_name, dimension, tags))

        output_dir = Path(output_directory).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        msh_path = output_dir / "mesh.msh"
        su2_path = output_dir / "mesh.su2"
        original_hash = _sha256(source)

        gmsh = _import_gmsh()
        gmsh.initialize()
        try:
            gmsh.model.add("cfdpipe_step_mesh")
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            imported_entities = gmsh.model.occ.importShapes(str(source))
            gmsh.model.occ.synchronize()
            if not imported_entities:
                raise GmshBridgeError(f"Gmsh imported no entities from {source}")

            available_by_dimension: dict[int, set[int]] = {}
            for _, dimension, _ in normalized_markers:
                if dimension not in available_by_dimension:
                    available_by_dimension[dimension] = {
                        int(tag) for _, tag in gmsh.model.getEntities(dimension)
                    }
            for name, dimension, tags in normalized_markers:
                missing = sorted(set(tags) - available_by_dimension[dimension])
                if missing:
                    raise GmshBridgeError(
                        f"marker {name!r} references missing entities {missing}"
                    )
                physical_tag = gmsh.model.addPhysicalGroup(dimension, tags)
                gmsh.model.setPhysicalName(dimension, physical_tag, name)

            reserved = {
                "generate_dimension",
                "number_options",
                "string_options",
            }
            number_options = dict(mesh_options.get("number_options", {}))
            string_options = dict(mesh_options.get("string_options", {}))
            for option_name, value in mesh_options.items():
                if option_name in reserved:
                    continue
                if isinstance(value, str):
                    string_options[option_name] = value
                else:
                    number_options[option_name] = value
            for option_name, value in number_options.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"Gmsh number option {option_name!r} is not numeric")
                if not math.isfinite(float(value)):
                    raise ValueError(f"Gmsh number option {option_name!r} is not finite")
                gmsh.option.setNumber(str(option_name), float(value))
            for option_name, value in string_options.items():
                gmsh.option.setString(str(option_name), str(value))

            inferred_dimension = max(int(dimension) for dimension, _ in imported_entities)
            generate_dimension = mesh_options.get(
                "generate_dimension", inferred_dimension
            )
            if (
                isinstance(generate_dimension, bool)
                or not isinstance(generate_dimension, int)
                or generate_dimension not in (1, 2, 3)
            ):
                raise ValueError("generate_dimension must be 1, 2, or 3")
            gmsh.model.mesh.generate(generate_dimension)
            gmsh.write(str(msh_path))
            gmsh.write(str(su2_path))
        finally:
            gmsh.finalize()

        if _sha256(source) != original_hash:
            raise GmshBridgeError(f"source STEP changed during import: {source}")
        return {
            "step_path": str(source),
            "output_directory": str(output_dir),
            "mesh_dimension": int(generate_dimension),
            "physical_groups": [name for name, _, _ in normalized_markers],
            "outputs": {
                msh_path.name: str(msh_path),
                su2_path.name: str(su2_path),
            },
            "output_sha256": {
                msh_path.name: _sha256(msh_path),
                su2_path.name: _sha256(su2_path),
            },
        }

    def step(
        self,
        step_path: PathLike,
        *,
        output_dir: PathLike | None = None,
        marker_mapping: Mapping[str, Mapping[str, Any]] | None = None,
        mesh_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility entry point requiring explicit external markers."""

        if output_dir is None or marker_mapping is None or mesh_options is None:
            raise ValueError(
                "STEP meshing requires output_dir, marker_mapping, and mesh_options; "
                "the production STEP is not run by the connection smoke command"
            )
        return self.build_step_mesh(
            step_path, output_dir, marker_mapping, mesh_options
        )


def build_step_mesh(
    step_path: PathLike,
    output_directory: PathLike,
    marker_mapping: Mapping[str, Mapping[str, Any]],
    mesh_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Module-level STEP entry point with externally supplied markers/options."""

    return GmshBridge().build_step_mesh(
        step_path, output_directory, marker_mapping, mesh_options
    )


__all__ = [
    "GmshBridge",
    "GmshBridgeError",
    "SU2MeshValidationError",
    "build_step_mesh",
    "validate_su2_mesh",
]
