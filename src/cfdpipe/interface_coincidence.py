"""Read-only Gmsh proof for complete coincidence of cross-volume STEP faces."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import stat
import traceback
from pathlib import Path
from typing import Any, Mapping, Sequence


PathLike = str | os.PathLike[str]
_SCHEMA_VERSION = "cfdpipe.interface_coincidence_probe.v1"


class InterfaceCoincidenceError(RuntimeError):
    """Raised when interface coincidence cannot be proven fail-closed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _snapshot(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "sha256": _sha256(path),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "read_only": _read_only(path),
    }


def _source(path: PathLike) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists() or candidate.is_symlink():
        raise InterfaceCoincidenceError(
            f"STEP source must be an existing non-symlink file: {candidate}"
        )
    source = candidate.resolve(strict=True)
    if source.suffix.casefold() not in {".step", ".stp"}:
        raise InterfaceCoincidenceError("STEP source must end in .step or .stp")
    if not source.is_file() or not _read_only(source):
        raise InterfaceCoincidenceError("STEP source must be an ordinary read-only file")
    return source


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise InterfaceCoincidenceError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise InterfaceCoincidenceError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise InterfaceCoincidenceError(f"{label} contains NaN or Inf")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise InterfaceCoincidenceError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise InterfaceCoincidenceError(f"{label} must be a positive integer") from error
    if result <= 0:
        raise InterfaceCoincidenceError(f"{label} must be a positive integer")
    return result


def _bound(value: object, label: str) -> float:
    if isinstance(value, (str, bytes)):
        raise InterfaceCoincidenceError(f"{label} is not a numeric bound")
    try:
        values = list(value)  # type: ignore[arg-type]
    except TypeError:
        return _finite(value, label)
    if len(values) != 1:
        raise InterfaceCoincidenceError(f"{label} must have exactly one value")
    return _finite(values[0], label)


def _point(values: Sequence[object], label: str) -> list[float]:
    result = [_finite(value, label) for value in values]
    if len(result) != 3:
        raise InterfaceCoincidenceError(f"{label} must have three coordinates")
    return result


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first, second)))


def _normal_dot(first: Sequence[float], second: Sequence[float]) -> float:
    first_length = math.sqrt(sum(value * value for value in first))
    second_length = math.sqrt(sum(value * value for value in second))
    if first_length == 0.0 or second_length == 0.0:
        raise InterfaceCoincidenceError("Gmsh returned a zero surface normal")
    return sum(left * right for left, right in zip(first, second)) / (
        first_length * second_length
    )


def _surface_direction(
    gmsh: Any,
    source_tag: int,
    target_tag: int,
    grid_size: int,
) -> dict[str, Any]:
    lower, upper = gmsh.model.getParametrizationBounds(2, source_tag)
    if len(lower) != 2 or len(upper) != 2:
        raise InterfaceCoincidenceError(
            f"surface {source_tag} has invalid parametrization bounds"
        )
    distances: list[float] = []
    dots: list[float] = []
    for first in range(grid_size):
        for second in range(grid_size):
            uv = [
                _finite(lower[0], "surface lower u")
                + (first + 0.5)
                / grid_size
                * (_finite(upper[0], "surface upper u") - _finite(lower[0], "surface lower u")),
                _finite(lower[1], "surface lower v")
                + (second + 0.5)
                / grid_size
                * (_finite(upper[1], "surface upper v") - _finite(lower[1], "surface lower v")),
            ]
            point = _point(
                list(gmsh.model.getValue(2, source_tag, uv)),
                f"surface {source_tag} sample",
            )
            if not bool(gmsh.model.isInside(2, source_tag, point, False)):
                continue
            closest_raw, target_uv_raw = gmsh.model.getClosestPoint(
                2, target_tag, point
            )
            closest = _point(
                list(closest_raw), f"surface {target_tag} closest point"
            )
            target_uv = [
                _finite(value, f"surface {target_tag} closest parameter")
                for value in list(target_uv_raw)
            ]
            if len(target_uv) != 2:
                raise InterfaceCoincidenceError(
                    f"surface {target_tag} closest parameter is not two-dimensional"
                )
            source_normal = _point(
                list(gmsh.model.getNormal(source_tag, uv)),
                f"surface {source_tag} parametric normal",
            )
            target_normal = _point(
                list(gmsh.model.getNormal(target_tag, target_uv)),
                f"surface {target_tag} parametric normal",
            )
            distances.append(_distance(point, closest))
            dots.append(_normal_dot(source_normal, target_normal))
    minimum_samples = max(4, grid_size * grid_size // 4)
    if len(distances) < minimum_samples:
        raise InterfaceCoincidenceError(
            f"surface {source_tag} produced only {len(distances)} valid samples; "
            f"need at least {minimum_samples}"
        )
    return {
        "source_surface_tag": source_tag,
        "target_surface_tag": target_tag,
        "sample_count": len(distances),
        "maximum_distance_m": max(distances),
        "mean_distance_m": sum(distances) / len(distances),
        "normal_dot_minimum": min(dots),
        "normal_dot_maximum": max(dots),
        "normal_dot_mean": sum(dots) / len(dots),
        "runtime_tags_are_audit_only": True,
    }


def _curve_points(gmsh: Any, tag: int, sample_count: int) -> list[list[float]]:
    lower_raw, upper_raw = gmsh.model.getParametrizationBounds(1, tag)
    lower = _bound(lower_raw, f"curve {tag} lower bound")
    upper = _bound(upper_raw, f"curve {tag} upper bound")
    if upper <= lower:
        raise InterfaceCoincidenceError(f"curve {tag} has invalid bounds")
    parameters = [
        lower + index / (sample_count - 1) * (upper - lower)
        for index in range(sample_count)
    ]
    values = [_finite(value, f"curve {tag} sample") for value in gmsh.model.getValue(1, tag, parameters)]
    if len(values) != sample_count * 3:
        raise InterfaceCoincidenceError(
            f"curve {tag} returned an unexpected sample count"
        )
    return [values[index : index + 3] for index in range(0, len(values), 3)]


def _sample_hausdorff(
    first: Sequence[Sequence[float]], second: Sequence[Sequence[float]]
) -> float:
    def directed(
        source: Sequence[Sequence[float]], target: Sequence[Sequence[float]]
    ) -> float:
        return max(min(_distance(a, b) for b in target) for a in source)

    return max(directed(first, second), directed(second, first))


def _surface_curves(gmsh: Any, surface_tag: int) -> list[int]:
    curves = sorted(
        {
            abs(_positive_int(tag, f"surface {surface_tag} boundary curve"))
            for dimension, tag in gmsh.model.getBoundary(
                [(2, surface_tag)], False, False, False
            )
            if int(dimension) == 1
        }
    )
    if not curves:
        raise InterfaceCoincidenceError(
            f"surface {surface_tag} has no boundary curves"
        )
    return curves


def _boundary_matching(
    gmsh: Any, first_tag: int, second_tag: int, sample_count: int
) -> dict[str, Any]:
    first_curves = _surface_curves(gmsh, first_tag)
    second_curves = _surface_curves(gmsh, second_tag)
    if len(first_curves) != len(second_curves):
        return {
            "bijective": False,
            "curve_count_first": len(first_curves),
            "curve_count_second": len(second_curves),
            "matches": [],
        }
    first_points = {
        tag: _curve_points(gmsh, tag, sample_count) for tag in first_curves
    }
    second_points = {
        tag: _curve_points(gmsh, tag, sample_count) for tag in second_curves
    }
    matches: list[dict[str, Any]] = []
    selected_second: set[int] = set()
    for first_curve in first_curves:
        distance, second_curve = min(
            (
                _sample_hausdorff(
                    first_points[first_curve], second_points[candidate]
                ),
                candidate,
            )
            for candidate in second_curves
        )
        selected_second.add(second_curve)
        matches.append(
            {
                "first_curve_tag": first_curve,
                "second_curve_tag": second_curve,
                "sample_hausdorff_m": distance,
                "first_length_m": _finite(
                    gmsh.model.occ.getMass(1, first_curve),
                    f"curve {first_curve} length",
                ),
                "second_length_m": _finite(
                    gmsh.model.occ.getMass(1, second_curve),
                    f"curve {second_curve} length",
                ),
                "runtime_tags_are_audit_only": True,
            }
        )
    bijective = len(selected_second) == len(second_curves)
    return {
        "bijective": bijective,
        "curve_count_per_surface": len(first_curves),
        "sample_count_per_curve": sample_count,
        "maximum_sample_hausdorff_m": max(
            item["sample_hausdorff_m"] for item in matches
        ),
        "matches": matches,
    }


def _new_json_path(path: PathLike) -> Path:
    result = Path(path).resolve()
    if result.suffix.casefold() != ".json":
        raise InterfaceCoincidenceError("probe output must end in .json")
    if any(part.casefold() == "config" for part in result.parts):
        raise InterfaceCoincidenceError("probe output cannot be written under config")
    if result.exists():
        raise InterfaceCoincidenceError(f"refusing to overwrite probe output: {result}")
    return result


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def inspect_interface_coincidence(
    step_path: PathLike,
    step_topology_path: PathLike,
    output_path: PathLike | None = None,
    *,
    grid_size: int = 17,
    boundary_sample_count: int = 201,
    surface_distance_tolerance_m: float = 1.0e-10,
    boundary_distance_tolerance_m: float = 1.0e-10,
    normal_dot_tolerance: float = 1.0e-9,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Prove complete coincident interfaces without meshing or CAD writes."""

    source = _source(step_path)
    topology_path = Path(step_topology_path).resolve()
    try:
        topology = json.loads(topology_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InterfaceCoincidenceError(
            f"cannot read STEP topology evidence: {error}"
        ) from error
    if not isinstance(topology, Mapping):
        raise InterfaceCoincidenceError("STEP topology evidence must be an object")
    before = _snapshot(source)
    if str(topology.get("source_sha256", "")).lower() != before["sha256"]:
        raise InterfaceCoincidenceError("STEP topology evidence source SHA256 is stale")
    contacts = topology.get("strong_contact_candidates")
    if not isinstance(contacts, list) or not contacts:
        raise InterfaceCoincidenceError("no strong contact candidates to prove")
    pair_tags: list[list[int]] = []
    for item in contacts:
        if not isinstance(item, Mapping) or not isinstance(item.get("surface_tags"), list):
            raise InterfaceCoincidenceError("invalid strong contact candidate")
        tags = [_positive_int(value, "contact surface tag") for value in item["surface_tags"]]
        if len(tags) != 2 or tags[0] == tags[1]:
            raise InterfaceCoincidenceError("contact candidate must contain two surfaces")
        pair_tags.append(tags)
    grid = _positive_int(grid_size, "grid_size")
    curve_samples = _positive_int(boundary_sample_count, "boundary_sample_count")
    if grid < 3 or curve_samples < 3:
        raise InterfaceCoincidenceError("probe sample counts must be at least three")
    surface_tolerance = _finite(
        surface_distance_tolerance_m, "surface distance tolerance"
    )
    boundary_tolerance = _finite(
        boundary_distance_tolerance_m, "boundary distance tolerance"
    )
    dot_tolerance = _finite(normal_dot_tolerance, "normal dot tolerance")
    if surface_tolerance <= 0.0 or boundary_tolerance <= 0.0 or not 0.0 < dot_tolerance < 1.0:
        raise InterfaceCoincidenceError("probe tolerances are invalid")
    gmsh = gmsh_module or importlib.import_module("gmsh")
    initialized = False
    failure: BaseException | None = None
    failure_traceback = ""
    pairs: list[dict[str, Any]] = []
    try:
        gmsh.initialize()
        initialized = True
        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        gmsh.model.add("cfdpipe_interface_coincidence")
        gmsh.model.occ.importShapes(str(source))
        gmsh.model.occ.synchronize()
        for first, second in pair_tags:
            directions = [
                _surface_direction(gmsh, first, second, grid),
                _surface_direction(gmsh, second, first, grid),
            ]
            maximum_distance = max(
                item["maximum_distance_m"] for item in directions
            )
            maximum_dot = max(
                item["normal_dot_maximum"] for item in directions
            )
            boundary = _boundary_matching(
                gmsh, first, second, curve_samples
            )
            boundary["distance_tolerance_m"] = boundary_tolerance
            full = (
                maximum_distance <= surface_tolerance
                and maximum_dot <= -1.0 + dot_tolerance
                and boundary.get("bijective") is True
                and boundary.get("maximum_sample_hausdorff_m", math.inf)
                <= boundary_tolerance
            )
            pairs.append(
                {
                    "surface_tags": [first, second],
                    "status": (
                        "FULL_GEOMETRIC_COINCIDENCE"
                        if full
                        else "INCOMPLETE_OR_AMBIGUOUS"
                    ),
                    "bidirectional_surface_projection": {
                        "direction_count": 2,
                        "sample_count_per_direction": min(
                            item["sample_count"] for item in directions
                        ),
                        "maximum_distance_m": maximum_distance,
                        "distance_tolerance_m": surface_tolerance,
                        "directions": directions,
                    },
                    "matched_parametric_normal_opposition": {
                        "opposite": maximum_dot <= -1.0 + dot_tolerance,
                        "maximum_dot_product": maximum_dot,
                        "dot_tolerance": dot_tolerance,
                        "used_only_for_pair_opposition_not_outward_direction": True,
                    },
                    "boundary_curve_matching": boundary,
                }
            )
    except BaseException as error:  # preserve the original traceback for evidence
        failure = error
        failure_traceback = traceback.format_exc()
    finally:
        if initialized:
            try:
                gmsh.finalize()
            except BaseException as finalize_error:
                if failure is None:
                    failure = finalize_error
                    failure_traceback = traceback.format_exc()
    after = _snapshot(source)
    if before != after:
        raise InterfaceCoincidenceError(
            "original STEP changed during interface coincidence probe"
        )
    if failure is not None:
        raise InterfaceCoincidenceError(
            "Gmsh interface coincidence probe failed; original traceback:\n"
            + failure_traceback
        ) from failure
    passed = bool(pairs) and all(
        item["status"] == "FULL_GEOMETRIC_COINCIDENCE" for item in pairs
    )
    report = {
        "schema_version": _SCHEMA_VERSION,
        "status": "PASS" if passed else "FAIL",
        "source_step_path": str(source),
        "source_step_sha256": before["sha256"],
        "source_snapshot_before": before,
        "source_snapshot_after": after,
        "source_unchanged": before == after,
        "gmsh_version": str(getattr(gmsh, "__version__", "unknown")),
        "gmsh_module_path": str(getattr(gmsh, "__file__", "unknown")),
        "input_parameters": {
            "grid_size": grid,
            "boundary_sample_count": curve_samples,
            "surface_distance_tolerance_m": surface_tolerance,
            "boundary_distance_tolerance_m": boundary_tolerance,
            "normal_dot_tolerance": dot_tolerance,
        },
        "pairs": pairs,
        "policy": {
            "gmsh_mesh_generated": False,
            "gmsh_write_called": False,
            "original_step_modified": False,
            "runtime_tags_are_audit_only": True,
            "parametric_normals_not_interpreted_as_outward": True,
        },
    }
    if output_path is not None:
        _write_new(_new_json_path(output_path), report)
    if not passed:
        raise InterfaceCoincidenceError(
            "one or more interface pairs failed complete-coincidence criteria"
        )
    return report


__all__ = ["InterfaceCoincidenceError", "inspect_interface_coincidence"]
