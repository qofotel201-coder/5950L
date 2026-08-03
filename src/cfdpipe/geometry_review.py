"""Read-only Gmsh diagnostics for a low-density STEP surface mesh.

This module is intentionally independent from the production meshing bridge.
It creates diagnostic physical names only, generates no volume elements and
never writes an SU2 mesh.
"""

from __future__ import annotations

from itertools import combinations
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import traceback
from typing import Any, Mapping, Sequence


PathLike = str | os.PathLike[str]
_HARD_TRIANGLE_LIMIT = 100_000
_STEP_SUFFIXES = {".step", ".stp"}


class GeometryReviewError(RuntimeError):
    """Raised when the diagnostic geometry review cannot be completed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _path_is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _existing_path_has_reparse_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        if component.exists() and _path_is_reparse(component):
            return True
    return False


def _path_is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return bool(attributes & read_only_flag)
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _validate_source(step_path: PathLike) -> Path:
    candidate = Path(step_path).expanduser()
    if not candidate.exists():
        raise ValueError(f"STEP path does not exist: {candidate}")
    if _existing_path_has_reparse_component(candidate):
        raise ValueError(
            f"diagnostic review refuses symlink/junction/reparse STEP: {candidate}"
        )
    source = candidate.resolve(strict=True)
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"STEP source is not an ordinary file: {source}")
    if source.suffix.lower() not in _STEP_SUFFIXES:
        raise ValueError(f"STEP source must end in .step or .stp: {source}")
    if not _path_is_read_only(source):
        raise ValueError(f"STEP source must be read-only: {source}")
    return source


def _validate_output(
    output_directory: PathLike,
    allowed_output_root: PathLike | None,
) -> Path:
    candidate = Path(output_directory).expanduser()
    allowed_candidate = (
        candidate
        if allowed_output_root is None
        else Path(allowed_output_root).expanduser()
    )
    if _existing_path_has_reparse_component(allowed_candidate):
        raise ValueError(
            f"allowed output root crosses a symlink/junction/reparse point: "
            f"{allowed_candidate}"
        )
    if _existing_path_has_reparse_component(candidate):
        raise ValueError(
            f"output crosses a symlink/junction/reparse point: {candidate}"
        )
    allowed = allowed_candidate.resolve(strict=False)
    output = candidate.resolve(strict=False)
    if output != allowed and allowed not in output.parents:
        raise ValueError(f"diagnostic output escapes {allowed}: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if _path_is_reparse(output) or not output.is_dir():
        raise ValueError(f"diagnostic output is not an ordinary directory: {output}")
    for name in (
        "diagnostic_surfaces.msh",
        "diagnostic_surfaces.vtk",
        "surface_types.json",
        "geometry_review_mesh_manifest.json",
        "gmsh_review.log",
    ):
        destination = output / name
        if destination.exists() and (
            _path_is_reparse(destination) or not destination.is_file()
        ):
            raise ValueError(f"refusing unsafe diagnostic output: {destination}")
    return output


def _finite_vector(values: Any, *, length: int, label: str) -> list[float]:
    try:
        converted = [float(value) for value in values]
    except (TypeError, ValueError) as error:
        raise GeometryReviewError(f"{label} is not numeric") from error
    if len(converted) != length:
        raise GeometryReviewError(
            f"{label} has {len(converted)} values; expected {length}"
        )
    if not all(math.isfinite(value) for value in converted):
        raise GeometryReviewError(f"{label} contains NaN or Inf")
    return converted


def _integer_vector(values: Any, *, label: str) -> list[int]:
    converted: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise GeometryReviewError(f"{label} contains a boolean tag")
        try:
            numeric = int(value)
        except (TypeError, ValueError) as error:
            raise GeometryReviewError(f"{label} contains an invalid tag") from error
        if numeric <= 0:
            raise GeometryReviewError(f"{label} contains a non-positive tag")
        converted.append(numeric)
    return converted


def _entity_tags(gmsh: Any, dimension: int) -> list[int]:
    tags = sorted(int(tag) for _, tag in gmsh.model.getEntities(dimension))
    if any(tag <= 0 for tag in tags) or len(tags) != len(set(tags)):
        raise GeometryReviewError(
            f"dimension {dimension} has invalid or duplicate entity tags"
        )
    return tags


def _surface_records(gmsh: Any, surface_tags: Sequence[int]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for tag in surface_tags:
        entity_type = str(gmsh.model.getType(2, tag))
        if not entity_type.strip():
            raise GeometryReviewError(f"surface {tag} has no Gmsh entity type")
        area = float(gmsh.model.occ.getMass(2, tag))
        if not math.isfinite(area) or area <= 0.0:
            raise GeometryReviewError(f"surface {tag} has invalid area: {area}")
        centroid = _finite_vector(
            gmsh.model.occ.getCenterOfMass(2, tag),
            length=3,
            label=f"surface {tag} centroid",
        )
        bounds = _finite_vector(
            gmsh.model.occ.getBoundingBox(2, tag),
            length=6,
            label=f"surface {tag} bounds",
        )
        if any(bounds[index] > bounds[index + 3] for index in range(3)):
            raise GeometryReviewError(f"surface {tag} has inverted bounds")
        adjacent, boundary_curves_raw = gmsh.model.getAdjacencies(2, tag)
        adjacent_volumes = sorted(set(_integer_vector(
            adjacent, label=f"surface {tag} adjacent volumes"
        )))
        boundary_curves = sorted(set(_integer_vector(
            boundary_curves_raw, label=f"surface {tag} boundary curves"
        )))
        records.append(
            {
                "entity_tag": int(tag),
                "entity_type": entity_type,
                "adjacent_volumes": adjacent_volumes,
                "boundary_curves": boundary_curves,
                "area": area,
                "centroid": centroid,
                "bounds": bounds,
                "area_m2": area,
                "centroid_m": centroid,
                "bounds_m": bounds,
            }
        )
    return records


def _volume_records(gmsh: Any, volume_tags: Sequence[int]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for tag in volume_tags:
        entity_type = str(gmsh.model.getType(3, tag))
        _, adjacent_faces_raw = gmsh.model.getAdjacencies(3, tag)
        adjacent_faces = sorted(set(_integer_vector(
            adjacent_faces_raw, label=f"volume {tag} adjacent surfaces"
        )))
        boundary_raw = gmsh.model.getBoundary(
            [(3, int(tag))], oriented=False, recursive=False
        )
        boundary_faces = sorted(
            {
                abs(int(face_tag))
                for dimension, face_tag in boundary_raw
                if int(dimension) == 2
            }
        )
        if adjacent_faces != boundary_faces:
            raise GeometryReviewError(
                f"volume {tag} adjacency and boundary surfaces disagree: "
                f"{adjacent_faces} != {boundary_faces}"
            )
        records.append(
            {
                "entity_tag": int(tag),
                "entity_type": entity_type,
                "boundary_surfaces": boundary_faces,
                "face_count": len(boundary_faces),
            }
        )
    return records


def _distance_record(
    get_distance: Any,
    dimension_a: int,
    tag_a: int,
    dimension_b: int,
    tag_b: int,
) -> dict[str, Any]:
    raw_result = get_distance(dimension_a, tag_a, dimension_b, tag_b)
    try:
        result = tuple(raw_result)
    except TypeError as error:
        raise GeometryReviewError(
            "OCC getDistance returned a non-sequence result"
        ) from error
    if len(result) == 7:
        # Gmsh 4.15 returns ``distance, x1, y1, z1, x2, y2, z2``.
        distance = result[0]
        nearest_a = result[1:4]
        nearest_b = result[4:7]
    elif len(result) == 3:
        # Retain compatibility with bindings that return two coordinate arrays.
        distance, nearest_a, nearest_b = result
    else:
        raise GeometryReviewError(
            "OCC getDistance returned an unsupported result length "
            f"{len(result)}; expected 3 or 7"
        )
    numeric_distance = float(distance)
    if not math.isfinite(numeric_distance) or numeric_distance < 0.0:
        raise GeometryReviewError(
            f"OCC distance for ({dimension_a}, {tag_a}) and "
            f"({dimension_b}, {tag_b}) is invalid"
        )
    point_a = _finite_vector(
        nearest_a, length=3, label="OCC nearest point A"
    )
    point_b = _finite_vector(
        nearest_b, length=3, label="OCC nearest point B"
    )
    return {
        "entity_a": [dimension_a, tag_a],
        "entity_b": [dimension_b, tag_b],
        "distance": numeric_distance,
        "distance_m": numeric_distance,
        "nearest_point_a": point_a,
        "nearest_point_b": point_b,
        "nearest_point_a_m": point_a,
        "nearest_point_b_m": point_b,
    }


def _bbox_gap(first: Sequence[float], second: Sequence[float]) -> float:
    squared = 0.0
    for axis in range(3):
        gap = max(first[axis] - second[axis + 3], second[axis] - first[axis + 3], 0.0)
        squared += gap * gap
    return math.sqrt(squared)


def _distance_evidence(
    gmsh: Any,
    surfaces: Sequence[Mapping[str, Any]],
    volumes: Sequence[Mapping[str, Any]],
    *,
    mesh_size_m: float,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    get_distance = getattr(gmsh.model.occ, "getDistance", None)
    evidence: dict[str, Any] = {
        "capability": "AVAILABLE" if callable(get_distance) else "UNAVAILABLE",
        "volume_pairs": [],
        "surface_pair_prescreen": [],
        "surface_pairs": [],
        "surface_prescreen_policy": {
            "maximum_relative_area_difference": 0.05,
            "maximum_bbox_gap_m": float(mesh_size_m),
            "centroid_limit": "0.5*(bbox_diagonal_a+bbox_diagonal_b)+mesh_size_m",
        },
    }
    if not callable(get_distance):
        warnings.append(
            "gmsh.model.occ.getDistance is unavailable; no distance values were fabricated"
        )
        return evidence, warnings

    for first, second in combinations(volumes, 2):
        evidence["volume_pairs"].append(
            _distance_record(
                get_distance,
                3,
                int(first["entity_tag"]),
                3,
                int(second["entity_tag"]),
            )
        )

    for first, second in combinations(surfaces, 2):
        owners_a = set(int(value) for value in first["adjacent_volumes"])
        owners_b = set(int(value) for value in second["adjacent_volumes"])
        if not owners_a or not owners_b or not owners_a.isdisjoint(owners_b):
            continue
        area_a = float(first["area_m2"])
        area_b = float(second["area_m2"])
        relative_area = abs(area_a - area_b) / max(area_a, area_b)
        bounds_a = [float(value) for value in first["bounds_m"]]
        bounds_b = [float(value) for value in second["bounds_m"]]
        gap = _bbox_gap(bounds_a, bounds_b)
        centroid_a = [float(value) for value in first["centroid_m"]]
        centroid_b = [float(value) for value in second["centroid_m"]]
        centroid_distance = math.dist(centroid_a, centroid_b)
        diagonal_a = math.dist(bounds_a[:3], bounds_a[3:])
        diagonal_b = math.dist(bounds_b[:3], bounds_b[3:])
        centroid_limit = 0.5 * (diagonal_a + diagonal_b) + mesh_size_m
        selected = (
            relative_area <= 0.05
            and gap <= mesh_size_m
            and centroid_distance <= centroid_limit
        )
        prescreen = {
            "surface_a": int(first["entity_tag"]),
            "surface_b": int(second["entity_tag"]),
            "relative_area_difference": relative_area,
            "bbox_gap_m": gap,
            "centroid_distance_m": centroid_distance,
            "centroid_limit_m": centroid_limit,
            "selected": selected,
        }
        evidence["surface_pair_prescreen"].append(prescreen)
        if selected:
            distance = _distance_record(
                get_distance,
                2,
                int(first["entity_tag"]),
                2,
                int(second["entity_tag"]),
            )
            distance["prescreen"] = prescreen
            evidence["surface_pairs"].append(distance)
    return evidence, warnings


def _mesh_evidence(
    gmsh: Any,
    *,
    max_triangles: int,
) -> dict[str, Any]:
    node_tags_raw, coordinates_raw, _ = gmsh.model.mesh.getNodes()
    node_tags = _integer_vector(node_tags_raw, label="mesh node tags")
    if not node_tags or len(node_tags) != len(set(node_tags)):
        raise GeometryReviewError("surface mesh has no nodes or duplicate node tags")
    coordinates = [float(value) for value in coordinates_raw]
    if len(coordinates) != 3 * len(node_tags):
        raise GeometryReviewError("surface mesh coordinate count is inconsistent")
    if not all(math.isfinite(value) for value in coordinates):
        raise GeometryReviewError("surface mesh coordinates contain NaN or Inf")

    volume_types, volume_tags, _ = gmsh.model.mesh.getElements(3)
    volume_element_count = sum(len(group) for group in volume_tags)
    if volume_element_count != 0 or any(len(group) for group in volume_tags):
        raise GeometryReviewError(
            f"diagnostic surface mesh contains {volume_element_count} 3D elements"
        )

    element_types, element_tags, element_nodes = gmsh.model.mesh.getElements(2)
    if not (
        len(element_types) == len(element_tags) == len(element_nodes)
    ):
        raise GeometryReviewError("Gmsh returned inconsistent surface element groups")
    triangle_count = 0
    node_set = set(node_tags)
    for element_type, tags_raw, connectivity_raw in zip(
        element_types, element_tags, element_nodes, strict=True
    ):
        tags = _integer_vector(tags_raw, label="surface element tags")
        if not tags:
            continue
        properties = gmsh.model.mesh.getElementProperties(int(element_type))
        name = str(properties[0])
        dimension = int(properties[1])
        order = int(properties[2])
        nodes_per_element = int(properties[3])
        if (
            dimension != 2
            or order != 1
            or nodes_per_element != 3
            or not name.lower().startswith("triangle")
        ):
            raise GeometryReviewError(
                f"diagnostic mesh contains non-first-order triangle type {name!r}"
            )
        connectivity = _integer_vector(
            connectivity_raw, label="triangle connectivity"
        )
        if len(connectivity) != len(tags) * 3:
            raise GeometryReviewError("triangle connectivity count is inconsistent")
        if any(node not in node_set for node in connectivity):
            raise GeometryReviewError("triangle connectivity references an unknown node")
        triangle_count += len(tags)
    if not 1 <= triangle_count <= max_triangles:
        raise GeometryReviewError(
            f"triangle count {triangle_count} is outside [1, {max_triangles}]"
        )
    if triangle_count > _HARD_TRIANGLE_LIMIT:
        raise GeometryReviewError(
            f"triangle count exceeds hard limit {_HARD_TRIANGLE_LIMIT}"
        )
    return {
        "node_count": len(node_tags),
        "triangle_count": triangle_count,
        "surface_element_dimension": 2,
        "volume_element_count": volume_element_count,
        "first_order_triangles_only": True,
        "finite_coordinates": True,
        "max_triangles": max_triangles,
    }


def _file_record(path: Path) -> dict[str, Any]:
    if not path.is_file() or _path_is_reparse(path) or path.stat().st_size <= 0:
        raise GeometryReviewError(f"diagnostic output is missing or unsafe: {path}")
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def build_diagnostic_surface_mesh(
    step_path: PathLike,
    output_directory: PathLike,
    *,
    allowed_output_root: PathLike | None = None,
    mesh_size_m: float = 0.20,
    max_triangles: int = 100000,
) -> dict[str, Any]:
    """Build a bounded two-dimensional diagnostic mesh from a read-only STEP."""

    if (
        isinstance(mesh_size_m, bool)
        or not isinstance(mesh_size_m, (int, float))
        or not math.isfinite(float(mesh_size_m))
        or float(mesh_size_m) <= 0.0
    ):
        raise ValueError("mesh_size_m must be finite and greater than zero")
    if (
        isinstance(max_triangles, bool)
        or not isinstance(max_triangles, int)
        or not 1 <= max_triangles <= _HARD_TRIANGLE_LIMIT
    ):
        raise ValueError(
            f"max_triangles must be an integer in [1, {_HARD_TRIANGLE_LIMIT}]"
        )

    source = _validate_source(step_path)
    output = _validate_output(output_directory, allowed_output_root)
    msh_path = output / "diagnostic_surfaces.msh"
    vtk_path = output / "diagnostic_surfaces.vtk"
    types_path = output / "surface_types.json"
    manifest_path = output / "geometry_review_mesh_manifest.json"
    log_path = output / "gmsh_review.log"

    started_at = _utc_now()
    source_hash_before = _sha256(source)
    source_size_before = source.stat().st_size
    manifest: dict[str, Any] = {
        "status": "FAIL",
        "started_at": started_at,
        "ended_at": None,
        "source_step_path": str(source),
        "source_size_bytes": source_size_before,
        "source_read_only_before": True,
        "source_read_only_after": None,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": None,
        "source_unchanged": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "target_geometry_unit": "M",
        "mesh_size_m": float(mesh_size_m),
        "max_triangles": max_triangles,
        "physical_groups": [],
        "surfaces": [],
        "volumes": [],
        "distance_evidence": None,
        "mesh": None,
        "warnings": [],
        "diagnostic_quality_status": None,
        "production_mesh_eligible": False,
        "outputs": {},
        "manifest_path": str(manifest_path),
        "error": None,
        "secondary_errors": [],
    }
    log_lines = [
        f"started_at: {started_at}",
        f"source_step_path: {source}",
        f"source_sha256_before: {source_hash_before}",
        "operation: diagnostic 2D surface mesh only; no CFD boundary roles",
        "target_geometry_unit: M",
    ]

    primary_error: BaseException | None = None
    primary_traceback: str | None = None
    types_payload: dict[str, Any] | None = None

    def record_error(error: BaseException) -> None:
        nonlocal primary_error, primary_traceback
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        if primary_error is None:
            primary_error = error
            primary_traceback = rendered
        else:
            manifest["secondary_errors"].append(
                {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": rendered,
                }
            )
        log_lines.append(rendered)

    gmsh: Any | None = None
    initialized = False
    logger_started = False
    try:
        import gmsh as imported_gmsh

        gmsh = imported_gmsh
        manifest["gmsh_version"] = str(
            getattr(gmsh, "__version__", getattr(gmsh, "GMSH_API_VERSION", "unknown"))
        )
        module_file = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            None if module_file is None else str(Path(module_file).resolve())
        )
        gmsh.initialize()
        initialized = True
        try:
            logger = getattr(gmsh, "logger", None)
            if logger is not None:
                logger.start()
                logger_started = True

            gmsh.model.add("cfdpipe_diagnostic_surface_mesh")
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            imported = gmsh.model.occ.importShapes(str(source))
            gmsh.model.occ.synchronize()
            if not imported:
                raise GeometryReviewError(f"Gmsh imported no entities from {source}")

            surface_tags = _entity_tags(gmsh, 2)
            volume_tags = _entity_tags(gmsh, 3)
            if not surface_tags:
                raise GeometryReviewError("STEP contains no surfaces")
            surfaces = _surface_records(gmsh, surface_tags)
            volumes = _volume_records(gmsh, volume_tags)
            manifest["surfaces"] = surfaces
            manifest["volumes"] = volumes

            distance_evidence, distance_warnings = _distance_evidence(
                gmsh, surfaces, volumes, mesh_size_m=float(mesh_size_m)
            )
            manifest["distance_evidence"] = distance_evidence
            manifest["warnings"].extend(distance_warnings)

            physical_tags: set[int] = set()
            physical_groups: list[dict[str, Any]] = []
            for tag in surface_tags:
                name = f"diagnostic_surface_{tag}"
                physical_tag = int(gmsh.model.addPhysicalGroup(2, [tag]))
                if physical_tag in physical_tags:
                    raise GeometryReviewError(
                        f"Gmsh reused diagnostic physical tag {physical_tag}"
                    )
                physical_tags.add(physical_tag)
                gmsh.model.setPhysicalName(2, physical_tag, name)
                physical_groups.append(
                    {
                        "name": name,
                        "dimension": 2,
                        "physical_tag": physical_tag,
                        "entity_tags": [tag],
                    }
                )
            manifest["physical_groups"] = physical_groups

            points = gmsh.model.getEntities(0)
            gmsh.model.mesh.setSize(points, float(mesh_size_m))
            for option_name, value in (
                ("Mesh.MeshSizeMin", float(mesh_size_m)),
                ("Mesh.MeshSizeMax", float(mesh_size_m)),
                ("Mesh.Algorithm", 6.0),
                ("Mesh.RecombineAll", 0.0),
                ("Mesh.ElementOrder", 1.0),
                ("Mesh.Binary", 0.0),
                ("Mesh.SaveAll", 0.0),
            ):
                gmsh.option.setNumber(option_name, value)
            gmsh.model.mesh.generate(2)
            manifest["mesh"] = _mesh_evidence(
                gmsh, max_triangles=max_triangles
            )

            gmsh.write(str(msh_path))
            gmsh.write(str(vtk_path))
            for generated in (msh_path, vtk_path):
                if generated.suffix.lower() == ".su2":
                    raise GeometryReviewError("diagnostic review must never write SU2")
            manifest["outputs"][msh_path.name] = _file_record(msh_path)
            manifest["outputs"][vtk_path.name] = _file_record(vtk_path)

            types_payload = {
                "status": "PASS",
                "source_step_path": str(source),
                "surfaces": surfaces,
                "volumes": volumes,
                "distance_evidence": distance_evidence,
                "warnings": list(manifest["warnings"]),
            }
        except BaseException as error:
            record_error(error)
        finally:
            if logger_started and gmsh is not None:
                try:
                    gmsh_messages = [str(line) for line in gmsh.logger.get()]
                    log_lines.extend(gmsh_messages)
                    manifest["warnings"].extend(
                        line
                        for line in gmsh_messages
                        if "warning" in line.lower()
                    )
                    gmsh_errors = [
                        line
                        for line in gmsh_messages
                        if line.lstrip().lower().startswith("error")
                    ]
                    if gmsh_errors:
                        record_error(
                            GeometryReviewError(
                                "Gmsh logger reported errors: " + " | ".join(gmsh_errors)
                            )
                        )
                except BaseException as error:
                    record_error(error)
                try:
                    gmsh.logger.stop()
                except BaseException as error:
                    record_error(error)
            try:
                gmsh.finalize()
            except BaseException as error:
                record_error(error)
            initialized = False
    except BaseException as error:
        if primary_error is None:
            record_error(error)
        elif initialized:
            # Defensive only: the owned session is normally finalized above.
            try:
                assert gmsh is not None
                gmsh.finalize()
            except BaseException as finalize_error:
                record_error(finalize_error)

    try:
        source_hash_after = _sha256(source)
        source_read_only_after = _path_is_read_only(source)
        source_size_after = source.stat().st_size
        source_unchanged = (
            source_hash_after == source_hash_before
            and source_size_after == source_size_before
            and source_read_only_after
        )
        manifest["source_sha256_after"] = source_hash_after
        manifest["source_read_only_after"] = source_read_only_after
        manifest["source_unchanged"] = source_unchanged
        if not source_unchanged:
            record_error(GeometryReviewError("source STEP changed during review"))
    except BaseException as error:
        record_error(error)

    manifest["diagnostic_quality_status"] = (
        "WARN" if manifest["warnings"] else "PASS"
    )
    if types_payload is not None:
        try:
            types_payload["status"] = "PASS" if primary_error is None else "FAIL"
            types_payload["warnings"] = list(manifest["warnings"])
            if primary_error is not None:
                types_payload["error"] = {
                    "type": type(primary_error).__name__,
                    "message": str(primary_error),
                }
            _write_json(types_path, types_payload)
            manifest["outputs"][types_path.name] = _file_record(types_path)
        except BaseException as error:
            record_error(error)

    manifest["ended_at"] = _utc_now()
    if primary_error is None:
        manifest["status"] = "PASS"
        log_lines.append("status: PASS")
    else:
        manifest["status"] = "FAIL"
        manifest["error"] = {
            "type": type(primary_error).__name__,
            "message": str(primary_error),
            "traceback": primary_traceback,
        }
        log_lines.append("status: FAIL")

    log_path.write_text("\n".join(log_lines).rstrip() + "\n", encoding="utf-8")
    manifest["outputs"][log_path.name] = _file_record(log_path)
    _write_json(manifest_path, manifest)

    if primary_error is not None:
        if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
            raise primary_error
        if isinstance(primary_error, GeometryReviewError):
            raise primary_error
        raise GeometryReviewError(str(primary_error)) from primary_error
    return manifest


__all__ = ["GeometryReviewError", "build_diagnostic_surface_mesh"]
