"""Independent proof that repaired BREP interfaces preserve source geometry.

The topology repair already proves that the two output volumes share two faces.
This module closes a different quality gate: in a fresh, read-only Gmsh session
it rematches the four source interface faces by stable fingerprints and proves
that each source pair and exactly one derived shared face cover the same
geometry.  It never meshes, writes CAD, or starts an external program.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import traceback
from typing import Any, Mapping, Sequence

from .geometry_repair import (
    GeometryRepairError,
    _collect_model,
    _is_read_only,
    _resolve_input_surface_identities,
    _resolve_input_volumes,
    _run_owned_session,
    _sha256,
    _snapshot,
    _validate_dependencies,
    _validate_shared_topology,
)
from .interface_coincidence import (
    InterfaceCoincidenceError,
    _curve_points,
    _distance,
    _finite,
    _normal_dot,
    _point,
    _sample_hausdorff,
    _surface_curves,
)


PathLike = str | os.PathLike[str]
_SCHEMA_VERSION = "cfdpipe.interface_persistence.v1"


class InterfacePersistenceError(RuntimeError):
    """Raised when source-to-derived interface persistence is not proven."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InterfacePersistenceError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise InterfacePersistenceError(f"{label} must be a JSON object")
    return value


def _input_file(
    value: PathLike, label: str, suffixes: set[str], *, read_only: bool = False
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts or raw.is_symlink():
        raise InterfacePersistenceError(f"{label} must not traverse or be a symlink")
    try:
        path = raw.resolve(strict=True)
    except OSError as error:
        raise InterfacePersistenceError(f"{label} does not exist: {raw}") from error
    if not path.is_file() or path.suffix.casefold() not in suffixes:
        raise InterfacePersistenceError(f"{label} has an invalid file type: {path}")
    if read_only and not _is_read_only(path):
        raise InterfacePersistenceError(f"{label} must be read-only: {path}")
    return path


def _new_output_path(value: PathLike) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise InterfacePersistenceError("output path must not contain '..'")
    path = Path(os.path.abspath(raw))
    if path.suffix.casefold() != ".json":
        raise InterfacePersistenceError("interface persistence output must be JSON")
    if any(part.casefold() == "config" for part in path.parts):
        raise InterfacePersistenceError("interface persistence output cannot be under config")
    if path.exists() or path.is_symlink():
        raise InterfacePersistenceError(f"refusing to overwrite output: {path}")
    return path


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise InterfacePersistenceError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise InterfacePersistenceError(f"{label} must be a positive integer") from error
    if result <= 0:
        raise InterfacePersistenceError(f"{label} must be a positive integer")
    return result


def _positive_finite(value: object, label: str) -> float:
    try:
        result = _finite(value, label)
    except InterfaceCoincidenceError as error:
        raise InterfacePersistenceError(str(error)) from error
    if result <= 0.0:
        raise InterfacePersistenceError(f"{label} must be greater than zero")
    return result


def _set_current(gmsh: Any, model_name: str) -> None:
    setter = getattr(gmsh.model, "setCurrent", None)
    if not callable(setter):
        raise InterfacePersistenceError("Gmsh model.setCurrent is required")
    setter(model_name)


def _surface_samples(
    gmsh: Any, model_name: str, surface_tag: int, grid_size: int
) -> list[dict[str, list[float]]]:
    _set_current(gmsh, model_name)
    lower, upper = gmsh.model.getParametrizationBounds(2, surface_tag)
    if len(lower) != 2 or len(upper) != 2:
        raise InterfacePersistenceError(
            f"surface {surface_tag} has invalid parametrization bounds"
        )
    lo = [_finite(value, "surface lower bound") for value in lower]
    hi = [_finite(value, "surface upper bound") for value in upper]
    samples: list[dict[str, list[float]]] = []
    for first in range(grid_size):
        for second in range(grid_size):
            uv = [
                lo[0] + (first + 0.5) / grid_size * (hi[0] - lo[0]),
                lo[1] + (second + 0.5) / grid_size * (hi[1] - lo[1]),
            ]
            point = _point(
                list(gmsh.model.getValue(2, surface_tag, uv)),
                f"surface {surface_tag} sample",
            )
            if not bool(gmsh.model.isInside(2, surface_tag, point, False)):
                continue
            normal = _point(
                list(gmsh.model.getNormal(surface_tag, uv)),
                f"surface {surface_tag} normal",
            )
            samples.append({"point": point, "normal": normal})
    minimum = max(4, grid_size * grid_size // 4)
    if len(samples) < minimum:
        raise InterfacePersistenceError(
            f"surface {surface_tag} produced {len(samples)} samples; need {minimum}"
        )
    return samples


def _surface_direction_across_models(
    gmsh: Any,
    source_model: str,
    source_tag: int,
    target_model: str,
    target_tag: int,
    grid_size: int,
    cache: dict[tuple[str, int, int], list[dict[str, list[float]]]],
) -> dict[str, Any]:
    cache_key = (source_model, source_tag, grid_size)
    samples = cache.get(cache_key)
    if samples is None:
        samples = _surface_samples(gmsh, source_model, source_tag, grid_size)
        cache[cache_key] = samples
    _set_current(gmsh, target_model)
    distances: list[float] = []
    absolute_dots: list[float] = []
    for sample in samples:
        closest_raw, target_uv_raw = gmsh.model.getClosestPoint(
            2, target_tag, sample["point"]
        )
        closest = _point(
            list(closest_raw), f"surface {target_tag} closest point"
        )
        target_uv = [
            _finite(value, f"surface {target_tag} closest parameter")
            for value in list(target_uv_raw)
        ]
        if len(target_uv) != 2:
            raise InterfacePersistenceError(
                f"surface {target_tag} closest parameter is not two-dimensional"
            )
        target_normal = _point(
            list(gmsh.model.getNormal(target_tag, target_uv)),
            f"surface {target_tag} closest normal",
        )
        distances.append(_distance(sample["point"], closest))
        absolute_dots.append(abs(_normal_dot(sample["normal"], target_normal)))
    return {
        "source_model": source_model,
        "source_surface_tag_audit": source_tag,
        "target_model": target_model,
        "target_surface_tag_audit": target_tag,
        "sample_count": len(samples),
        "maximum_distance_m": max(distances),
        "mean_distance_m": sum(distances) / len(distances),
        "minimum_absolute_normal_dot": min(absolute_dots),
        "runtime_tags_are_audit_only": True,
    }


def _curve_inventory(
    gmsh: Any, model_name: str, surface_tag: int, sample_count: int
) -> dict[int, dict[str, Any]]:
    _set_current(gmsh, model_name)
    result: dict[int, dict[str, Any]] = {}
    for curve_tag in _surface_curves(gmsh, surface_tag):
        result[curve_tag] = {
            "points": _curve_points(gmsh, curve_tag, sample_count),
            "length_m": _finite(
                gmsh.model.occ.getMass(1, curve_tag), f"curve {curve_tag} length"
            ),
        }
    return result


def _boundary_matching_across_models(
    gmsh: Any,
    first_model: str,
    first_surface: int,
    second_model: str,
    second_surface: int,
    sample_count: int,
) -> dict[str, Any]:
    first = _curve_inventory(gmsh, first_model, first_surface, sample_count)
    second = _curve_inventory(gmsh, second_model, second_surface, sample_count)
    if len(first) != len(second):
        return {
            "bijective": False,
            "curve_count_first": len(first),
            "curve_count_second": len(second),
            "sample_count_per_curve": sample_count,
            "matches": [],
        }
    distances = {
        (left, right): _sample_hausdorff(
            first[left]["points"], second[right]["points"]
        )
        for left in first
        for right in second
    }
    left_choice = {
        left: min(second, key=lambda right: (distances[(left, right)], right))
        for left in first
    }
    right_choice = {
        right: min(first, key=lambda left: (distances[(left, right)], left))
        for right in second
    }
    bijective = (
        len(set(left_choice.values())) == len(second)
        and all(right_choice[right] == left for left, right in left_choice.items())
    )
    matches = [
        {
            "first_curve_tag_audit": left,
            "second_curve_tag_audit": right,
            "sample_hausdorff_m": distances[(left, right)],
            "first_length_m": first[left]["length_m"],
            "second_length_m": second[right]["length_m"],
            "mutual_nearest": right_choice[right] == left,
            "runtime_tags_are_audit_only": True,
        }
        for left, right in sorted(left_choice.items())
    ]
    return {
        "bijective": bijective,
        "curve_count_per_surface": len(first),
        "sample_count_per_curve": sample_count,
        "maximum_sample_hausdorff_m": max(
            item["sample_hausdorff_m"] for item in matches
        ),
        "matches": matches,
    }


def _evaluate_pair_candidate(
    gmsh: Any,
    *,
    source_model: str,
    source_tags: Sequence[int],
    derived_model: str,
    derived_tag: int,
    grid_size: int,
    boundary_sample_count: int,
    surface_tolerance_m: float,
    boundary_tolerance_m: float,
    normal_tolerance: float,
    cache: dict[tuple[str, int, int], list[dict[str, list[float]]]],
) -> dict[str, Any]:
    copy_results: list[dict[str, Any]] = []
    for source_tag in source_tags:
        directions = [
            _surface_direction_across_models(
                gmsh,
                source_model,
                source_tag,
                derived_model,
                derived_tag,
                grid_size,
                cache,
            ),
            _surface_direction_across_models(
                gmsh,
                derived_model,
                derived_tag,
                source_model,
                source_tag,
                grid_size,
                cache,
            ),
        ]
        boundary = _boundary_matching_across_models(
            gmsh,
            source_model,
            source_tag,
            derived_model,
            derived_tag,
            boundary_sample_count,
        )
        maximum_distance = max(item["maximum_distance_m"] for item in directions)
        minimum_alignment = min(
            item["minimum_absolute_normal_dot"] for item in directions
        )
        copy_pass = (
            maximum_distance <= surface_tolerance_m
            and minimum_alignment >= 1.0 - normal_tolerance
            and boundary.get("bijective") is True
            and float(boundary.get("maximum_sample_hausdorff_m", math.inf))
            <= boundary_tolerance_m
        )
        copy_results.append(
            {
                "source_surface_tag_audit": source_tag,
                "derived_surface_tag_audit": derived_tag,
                "status": "PASS" if copy_pass else "FAIL",
                "bidirectional_surface_projection": {
                    "status": (
                        "PASS" if maximum_distance <= surface_tolerance_m else "FAIL"
                    ),
                    "maximum_distance_m": maximum_distance,
                    "distance_tolerance_m": surface_tolerance_m,
                    "directions": directions,
                },
                "absolute_parametric_normal_alignment": {
                    "status": (
                        "PASS"
                        if minimum_alignment >= 1.0 - normal_tolerance
                        else "FAIL"
                    ),
                    "minimum_absolute_dot_product": minimum_alignment,
                    "dot_tolerance": normal_tolerance,
                    "orientation_sign_proven_separately_by_volume_boundary": True,
                },
                "boundary_curve_matching": {
                    **boundary,
                    "status": (
                        "PASS"
                        if boundary.get("bijective") is True
                        and float(
                            boundary.get("maximum_sample_hausdorff_m", math.inf)
                        )
                        <= boundary_tolerance_m
                        else "FAIL"
                    ),
                    "distance_tolerance_m": boundary_tolerance_m,
                },
            }
        )
    passed = len(copy_results) == 2 and all(
        item["status"] == "PASS" for item in copy_results
    )
    return {
        "derived_shared_surface_tag_audit": derived_tag,
        "status": "FULL_COVERAGE" if passed else "NOT_A_MATCH",
        "source_copy_count": len(copy_results),
        "source_copy_checks": copy_results,
        "runtime_tags_are_audit_only": True,
    }


def _select_unique_matches(
    rows: Sequence[Mapping[str, Any]],
    pair_fingerprint_ids: Sequence[str],
    shared_tags: Sequence[int],
) -> dict[str, int]:
    result: dict[str, int] = {}
    used: set[int] = set()
    for fingerprint_id in pair_fingerprint_ids:
        candidates = [
            int(item["derived_shared_surface_tag_audit"])
            for item in rows
            if item.get("interface_pair_fingerprint_id") == fingerprint_id
            and item.get("status") == "FULL_COVERAGE"
        ]
        if len(candidates) != 1:
            raise InterfacePersistenceError(
                f"interface pair {fingerprint_id} has {len(candidates)} full matches"
            )
        if candidates[0] in used:
            raise InterfacePersistenceError(
                "two stable interface pairs selected the same derived shared face"
            )
        used.add(candidates[0])
        result[fingerprint_id] = candidates[0]
    if used != set(int(value) for value in shared_tags):
        raise InterfacePersistenceError(
            "stable interface matches do not exhaust derived shared faces"
        )
    return result


def _validate_repair_documents(
    *,
    source: Path,
    derived: Path,
    project: Path,
    repair_manifest_path: Path,
    shared_interface_path: Path,
    dependencies: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if _sha256(project) != dependencies.get("source_project_sha256"):
        raise InterfacePersistenceError(
            "current project.toml no longer matches the confirmed identity contract"
        )
    manifest = _load_json(repair_manifest_path, "repair manifest")
    pipeline = manifest.get("pipeline_geometry")
    reimport = manifest.get("reimport_validation")
    source_record = manifest.get("source")
    if (
        manifest.get("status") != "PASS"
        or manifest.get("source_unchanged") is not True
        or not isinstance(source_record, Mapping)
        or source_record.get("sha256") != _sha256(source)
        or not isinstance(pipeline, Mapping)
        or Path(str(pipeline.get("path", ""))).resolve(strict=True) != derived
        or pipeline.get("sha256") != _sha256(derived)
        or pipeline.get("pipeline_eligible") is not True
        or not isinstance(reimport, Mapping)
        or not isinstance(reimport.get("brep"), Mapping)
        or reimport["brep"].get("status") != "PASS"
        or reimport["brep"].get("pipeline_eligible") is not True
    ):
        raise InterfacePersistenceError("repair manifest is not a current pipeline PASS")
    shared = _load_json(shared_interface_path, "shared-interface evidence")
    brep_reimport = shared.get("brep_reimport")
    if (
        shared.get("status") != "PASS"
        or shared.get("pipeline_geometry_format") != "brep"
        or not isinstance(brep_reimport, Mapping)
        or brep_reimport.get("status") != "PASS"
        or brep_reimport.get("shared_patch_count") != 2
    ):
        raise InterfacePersistenceError(
            "shared-interface evidence is not a two-patch BREP PASS"
        )
    return manifest, shared


def validate_interface_persistence(
    source_step_path: PathLike,
    derived_brep_path: PathLike,
    output_path: PathLike,
    *,
    project_path: PathLike,
    step_topology_path: PathLike,
    interface_evidence_path: PathLike,
    boundary_candidates_path: PathLike,
    confirmation_document_path: PathLike,
    confirmation_validation_path: PathLike,
    original_surface_catalog_path: PathLike,
    original_volume_catalog_path: PathLike,
    repair_manifest_path: PathLike,
    shared_interface_path: PathLike,
    grid_size: int = 17,
    boundary_sample_count: int = 201,
    surface_distance_tolerance_m: float = 1.0e-9,
    boundary_distance_tolerance_m: float = 1.0e-9,
    normal_dot_tolerance: float = 1.0e-8,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Prove a stable, complete, bijective source-interface to BREP mapping."""

    source = _input_file(
        source_step_path, "source STEP", {".step", ".stp"}, read_only=True
    )
    derived = _input_file(
        derived_brep_path, "derived BREP", {".brep"}, read_only=True
    )
    project = _input_file(project_path, "project configuration", {".toml"})
    output = _new_output_path(output_path)
    evidence_paths = {
        "step_topology": _input_file(step_topology_path, "STEP topology", {".json"}),
        "interface_completeness": _input_file(
            interface_evidence_path, "interface completeness", {".json"}
        ),
        "boundary_candidates": _input_file(
            boundary_candidates_path, "boundary candidates", {".json"}
        ),
        "confirmation": _input_file(
            confirmation_document_path, "marker confirmation", {".json"}
        ),
        "confirmation_validation": _input_file(
            confirmation_validation_path, "confirmation validation", {".json"}
        ),
        "surface_catalog": _input_file(
            original_surface_catalog_path, "original surface catalog", {".csv"}
        ),
        "volume_catalog": _input_file(
            original_volume_catalog_path, "original volume catalog", {".csv"}
        ),
        "repair_manifest": _input_file(
            repair_manifest_path, "repair manifest", {".json"}
        ),
        "shared_interface": _input_file(
            shared_interface_path, "shared-interface evidence", {".json"}
        ),
    }
    grid = _positive_int(grid_size, "grid_size")
    curve_samples = _positive_int(
        boundary_sample_count, "boundary_sample_count"
    )
    if grid < 3 or curve_samples < 3:
        raise InterfacePersistenceError("sample counts must be at least three")
    surface_tolerance = _positive_finite(
        surface_distance_tolerance_m, "surface distance tolerance"
    )
    boundary_tolerance = _positive_finite(
        boundary_distance_tolerance_m, "boundary distance tolerance"
    )
    normal_tolerance = _positive_finite(
        normal_dot_tolerance, "normal dot tolerance"
    )
    if normal_tolerance >= 1.0:
        raise InterfacePersistenceError("normal dot tolerance must be less than one")
    try:
        dependencies = _validate_dependencies(
            source=source,
            step_topology_path=evidence_paths["step_topology"],
            interface_evidence_path=evidence_paths["interface_completeness"],
            boundary_candidates_path=evidence_paths["boundary_candidates"],
            confirmation_document_path=evidence_paths["confirmation"],
            confirmation_validation_path=evidence_paths[
                "confirmation_validation"
            ],
            surface_catalog_path=evidence_paths["surface_catalog"],
            volume_catalog_path=evidence_paths["volume_catalog"],
        )
    except GeometryRepairError as error:
        raise InterfacePersistenceError(str(error)) from error
    repair_manifest, shared_interface = _validate_repair_documents(
        source=source,
        derived=derived,
        project=project,
        repair_manifest_path=evidence_paths["repair_manifest"],
        shared_interface_path=evidence_paths["shared_interface"],
        dependencies=dependencies,
    )
    source_before = _snapshot(source)
    derived_before = _snapshot(derived)
    started_at = _utc_now()
    gmsh = gmsh_module or importlib.import_module("gmsh")
    log_lines: list[str] = []
    source_model = "cfdpipe_interface_source"
    derived_model = "cfdpipe_interface_derived"

    def action() -> dict[str, Any]:
        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        gmsh.model.occ.importShapes(str(source))
        gmsh.model.occ.synchronize()
        source_catalog = _collect_model(gmsh)
        resolved_volumes = _resolve_input_volumes(
            source_catalog, dependencies["solid_references"]
        )
        source_identities = _resolve_input_surface_identities(
            source_catalog, resolved_volumes, dependencies
        )
        gmsh.model.add(derived_model)
        _set_current(gmsh, derived_model)
        gmsh.model.occ.importShapes(str(derived))
        gmsh.model.occ.synchronize()
        derived_catalog = _collect_model(gmsh)
        topology = _validate_shared_topology(derived_catalog)
        shared_tags = [int(value) for value in topology["shared_surface_tags_audit"]]
        if len(shared_tags) != 2:
            raise InterfacePersistenceError(
                "current derived BREP does not contain exactly two shared faces"
            )
        pair_evidence = source_identities["interface_pair_matches_audit"]
        source_pairs = source_identities["interface_pairs"]
        if len(pair_evidence) != 2 or len(source_pairs) != 2:
            raise InterfacePersistenceError(
                "stable source identity did not resolve exactly two interface pairs"
            )
        cache: dict[
            tuple[str, int, int], list[dict[str, list[float]]]
        ] = {}
        matrix: list[dict[str, Any]] = []
        for evidence, source_tags in zip(pair_evidence, source_pairs, strict=True):
            fingerprint_id = str(evidence["interface_pair_fingerprint_id"])
            for shared_tag in shared_tags:
                matrix.append(
                    {
                        "interface_pair_fingerprint_id": fingerprint_id,
                        "member_surface_fingerprint_ids": list(
                            evidence["member_surface_fingerprint_ids"]
                        ),
                        "source_surface_tags_audit": list(source_tags),
                        **_evaluate_pair_candidate(
                            gmsh,
                            source_model=source_model,
                            source_tags=source_tags,
                            derived_model=derived_model,
                            derived_tag=shared_tag,
                            grid_size=grid,
                            boundary_sample_count=curve_samples,
                            surface_tolerance_m=surface_tolerance,
                            boundary_tolerance_m=boundary_tolerance,
                            normal_tolerance=normal_tolerance,
                            cache=cache,
                        ),
                    }
                )
        pair_ids = [
            str(item["interface_pair_fingerprint_id"]) for item in pair_evidence
        ]
        selected = _select_unique_matches(matrix, pair_ids, shared_tags)
        lineages: list[dict[str, Any]] = []
        for pair in pair_evidence:
            fingerprint_id = str(pair["interface_pair_fingerprint_id"])
            derived_tag = selected[fingerprint_id]
            row = next(
                item
                for item in matrix
                if item["interface_pair_fingerprint_id"] == fingerprint_id
                and item["derived_shared_surface_tag_audit"] == derived_tag
            )
            orientations = topology["shared_orientations_audit"].get(
                str(derived_tag),
                topology["shared_orientations_audit"].get(derived_tag),
            )
            if not isinstance(orientations, Mapping) or sorted(
                int(value) for value in orientations.values()
            ) != [-1, 1]:
                raise InterfacePersistenceError(
                    f"derived shared face {derived_tag} lacks opposite volume orientation"
                )
            lineages.append(
                {
                    "status": "PASS",
                    "interface_pair_fingerprint_id": fingerprint_id,
                    "member_surface_fingerprint_ids": list(
                        pair["member_surface_fingerprint_ids"]
                    ),
                    "source_surface_tags_audit": list(
                        row["source_surface_tags_audit"]
                    ),
                    "derived_shared_surface_tag_audit": derived_tag,
                    "patch_count": 1,
                    "coverage": row,
                    "shared_topology": {
                        "adjacent_volume_count": 2,
                        "oriented_boundary_signs": sorted(
                            int(value) for value in orientations.values()
                        ),
                        "status": "PASS",
                    },
                    "overlap_and_exhaustion": {
                        "status": "PASS",
                        "method": "single_patch_lineage_has_no_internal_overlap",
                        "all_derived_shared_faces_exhausted_by_global_bijection": True,
                    },
                    "runtime_tags_are_audit_only": True,
                }
            )
        return {
            "source_identity_resolution": source_identities,
            "derived_topology": topology,
            "candidate_matrix": matrix,
            "selected_bijection": selected,
            "lineages": lineages,
        }

    try:
        session_result = _run_owned_session(
            gmsh, source_model, action, log_lines
        )
    except BaseException as error:
        raise InterfacePersistenceError(
            "Gmsh interface persistence validation failed; original traceback:\n"
            + traceback.format_exc()
        ) from error
    source_after = _snapshot(source)
    derived_after = _snapshot(derived)
    if source_after != source_before or derived_after != derived_before:
        raise InterfacePersistenceError(
            "source STEP or derived BREP changed during read-only validation"
        )
    report = {
        "schema_version": _SCHEMA_VERSION,
        "status": "PASS",
        "started_at_utc": started_at,
        "ended_at_utc": _utc_now(),
        "source_step": {
            "path": str(source),
            "before": source_before,
            "after": source_after,
            "unchanged": True,
        },
        "derived_brep": {
            "path": str(derived),
            "before": derived_before,
            "after": derived_after,
            "unchanged": True,
            "pipeline_eligible": True,
        },
        "project": {"path": str(project), "sha256": _sha256(project)},
        "gmsh_version": str(getattr(gmsh, "__version__", "unknown")),
        "gmsh_module_path": str(getattr(gmsh, "__file__", "unknown")),
        "input_parameters": {
            "grid_size": grid,
            "boundary_sample_count": curve_samples,
            "surface_distance_tolerance_m": surface_tolerance,
            "boundary_distance_tolerance_m": boundary_tolerance,
            "normal_dot_tolerance": normal_tolerance,
        },
        "dependencies": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in evidence_paths.items()
        },
        "repair_manifest_status": repair_manifest["status"],
        "shared_interface_status": shared_interface["status"],
        **session_result,
        "validation": {
            "stable_source_identity_recomputed": True,
            "matching_uses_old_runtime_tags": False,
            "source_copy_to_derived_projection_complete": True,
            "boundary_curve_geometry_complete": True,
            "absolute_parametric_normals_aligned": True,
            "derived_oriented_boundaries_opposite": True,
            "shared_face_mapping_bijective_and_exhaustive": True,
            "no_patch_overlap": True,
        },
        "gmsh_log": log_lines,
        "lifecycle": {
            "owned_session": True,
            "initialize_succeeded": True,
            "logger_collection_attempted": True,
            "finalize_succeeded": True,
            "success_cannot_be_returned_after_finalize_failure": True,
        },
        "policy": {
            "source_step_modified": False,
            "derived_brep_modified": False,
            "mesh_generated": False,
            "cad_written": False,
            "physical_groups_created": False,
            "external_process_started": False,
            "gui_started": False,
        },
    }
    _write_new_json(output, report)
    return report


__all__ = [
    "InterfacePersistenceError",
    "validate_interface_persistence",
]
