"""Hash-bound contract for design-point coarse-mesh calibration and gating.

This module separates the production/coarse contract from the existing
300k/15-layer smoke contract, validates every upstream source and evidence
file, derives the thicker prism schedule, and supplies the bounded calibration
builder used only by the isolated worker.
"""

from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import tomllib
import traceback
from typing import Any, Mapping
from uuid import uuid4

from .boundary_layer_design import (
    BoundaryLayerDesignError,
    design_boundary_layer_stack,
)
from .boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
    validate_boundary_layer_schedule_binding,
)
from .boundary_layer_smoke import (
    BoundaryLayerSmokeError,
    RealProjectBoundaryLayerStrategy,
    _audit_signature,
    _call_audit,
    _finalize_deferred_gmsh_log_diagnostics,
    _quality_gate_signature,
    _reject_fatal_gmsh_log,
    _session,
    _strategy_evidence,
    normalize_boundary_layer_smoke_config,
)
from .mesh_audit import audit_su2_mesh
from .coarse_projection_evidence import (
    CoarseProjectionEvidenceError,
    validate_projection_evidence_record,
)
from .resource_gate import (
    MINIMUM_CALIBRATION_SAMPLES,
    OS_RESERVE_BYTES,
    SAFETY_FACTOR,
)


_SCHEMA = "cfdpipe.coarse_mesh.v1"
_COARSE_REFINEMENT_GROUP_NAMES = {"rear_top_shell_plc_recovery"}
_REFINEMENT_GROUP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CLEARANCE_EVIDENCE_CONTRACT_SCHEMA = (
    "cfdpipe.boundary_layer_clearance_evidence_contract.v1"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseMeshError(RuntimeError):
    """Raised whenever coarse-mesh authorization evidence is incomplete."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _make_clearance_evidence_contract(
    *,
    pipeline_brep_path: Path,
    pipeline_brep_sha256: str,
    markers_path: Path,
    markers_sha256: str,
    marker_pipeline_geometry_sha256: str,
    coordinate_absolute_tolerance_m: float,
    required_total_thickness_m: float,
    wall_physical_name: str,
    wall_surface_fingerprints: list[str],
    direction_probe_distance_m: float,
) -> dict[str, Any]:
    """Return the stable input contract for a no-mesh clearance audit.

    Deliberately excluded inputs include cell-count targets, worker memory
    limits, calibration sizes, mesh-quality thresholds and the downstream
    local-stack clearance fraction.  None of those values participates in the
    read-only BREP ray audit, so changing them must not invalidate already
    collected geometric evidence.
    """

    fingerprints = sorted(str(value).casefold() for value in wall_surface_fingerprints)
    if (
        _SHA256.fullmatch(str(pipeline_brep_sha256).casefold()) is None
        or _SHA256.fullmatch(str(markers_sha256).casefold()) is None
        or marker_pipeline_geometry_sha256 != pipeline_brep_sha256
        or len(fingerprints) != 48
        or len(set(fingerprints)) != 48
        or any(_SHA256.fullmatch(value) is None for value in fingerprints)
        or not wall_physical_name
    ):
        raise CoarseMeshError("clearance evidence identity is incomplete")
    coordinate_tolerance = _finite(
        coordinate_absolute_tolerance_m,
        "marker coordinate tolerance",
        positive=True,
    )
    required_thickness = _finite(
        required_total_thickness_m,
        "clearance required total thickness",
        positive=True,
    )
    probe_distance = _finite(
        direction_probe_distance_m,
        "clearance direction probe distance",
        positive=True,
    )
    return {
        "schema": _CLEARANCE_EVIDENCE_CONTRACT_SCHEMA,
        "source_geometry": {
            "path": str(pipeline_brep_path),
            "sha256": str(pipeline_brep_sha256).casefold(),
            "required_suffix": ".brep",
            "read_only_required": True,
            "links_allowed": False,
        },
        "marker_config": {
            "path": str(markers_path),
            "sha256": str(markers_sha256).casefold(),
            "pipeline_geometry_sha256": str(
                marker_pipeline_geometry_sha256
            ).casefold(),
            "coordinate_absolute_tolerance_m": coordinate_tolerance,
        },
        "wall_selection": {
            "physical_name": str(wall_physical_name),
            "stable_surface_fingerprint_count": 48,
            "stable_surface_fingerprints": fingerprints,
        },
        "clearance_requirement": {
            "required_total_thickness_m": required_thickness,
        },
        "audit_policy": {
            "samples_per_surface": 5,
            "total_sample_count": 240,
            "direction_probe_distance_m": probe_distance,
            "direction_probe_coordinate_tolerance_multipliers": [
                5.0,
                50.0,
                500.0,
            ],
            "maximum_search_distance_volume_diagonal_multiplier": 2.0,
            "maximum_probe_iterations": 96,
            "absolute_tolerance_policy": (
                "min(marker_coordinate_absolute_tolerance_m,"
                "required_total_thickness_m*1e-6)"
            ),
            "transition_policy": (
                "doubling_then_bisection_first_detected_inside_to_outside"
            ),
            "nonconvex_first_exit_guaranteed": False,
        },
        "scope": {
            "read_only_geometry_audit": True,
            "mesh_generated": False,
            "gmsh_write_called": False,
            "gui_started": False,
            "su2_called": False,
            "paraview_called": False,
            "extrusion_feasibility_claimed": False,
            "production_mesh_eligible": False,
        },
    }


def _toml(path: Path, label: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CoarseMeshError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise CoarseMeshError(f"{label} is not a TOML table")
    return value


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseMeshError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise CoarseMeshError(f"{label} is not a JSON object")
    return value


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise CoarseMeshError(f"{label} must be finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseMeshError(f"{label} must be finite numeric data") from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        raise CoarseMeshError(f"{label} must be finite and positive")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseMeshError(f"{label} must be a positive integer")
    return value


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    return not bool(metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _trusted_file(root: Path, relative: str, label: str) -> Path:
    lexical = Path(relative)
    if lexical.is_absolute() or ".." in lexical.parts:
        raise CoarseMeshError(f"{label} path is unsafe")
    try:
        resolved = (root / lexical).resolve(strict=True)
    except OSError as error:
        raise CoarseMeshError(f"{label} path does not exist") from error
    if root not in resolved.parents or not resolved.is_file():
        raise CoarseMeshError(f"{label} path is outside the repository")
    return resolved


def _evidence_file(
    root: Path, config_root: Path, record: Mapping[str, Any], label: str
) -> tuple[Path, dict[str, Any]]:
    if set(record) not in (
        {"path", "sha256"},
        {"path", "sha256", "maximum_yplus", "first_layer_height_m"},
    ):
        raise CoarseMeshError(f"{label} evidence record is incomplete")
    requested = Path(str(record.get("path", "")))
    if requested.is_absolute() or not requested.parts:
        raise CoarseMeshError(f"{label} evidence path is unsafe")
    try:
        path = (config_root / requested).resolve(strict=True)
    except OSError as error:
        raise CoarseMeshError(f"{label} evidence does not exist") from error
    if root not in path.parents or not path.is_file() or path.suffix.casefold() != ".json":
        raise CoarseMeshError(f"{label} evidence path is untrusted")
    configured = str(record.get("sha256", "")).casefold()
    if _SHA256.fullmatch(configured) is None or _sha256(path) != configured:
        raise CoarseMeshError(f"{label} evidence SHA-256 is stale")
    return path, _json(path, label)


def _load_case(path: Path, case_id: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            records = [dict(row) for row in csv.DictReader(stream)]
    except OSError as error:
        raise CoarseMeshError(f"cannot read cases.csv: {error}") from error
    matching = [record for record in records if record.get("case_id") == case_id]
    if len(matching) != 1:
        raise CoarseMeshError("coarse case_id is not unique in cases.csv")
    record = matching[0]
    try:
        return {
            **record,
            "mach": float(record["mach"]),
            "altitude_km": float(record["altitude_km"]),
            "alpha_deg": float(record["alpha_deg"]),
            "beta_deg": float(record["beta_deg"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise CoarseMeshError("selected coarse case is not parseable") from error


def normalize_coarse_mesh_config(
    document: Mapping[str, Any], *, repository_root: str | os.PathLike[str]
) -> dict[str, Any]:
    """Validate all production-mesh planning inputs without launching Gmsh."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise CoarseMeshError("repository root is not a directory")
    expected_root = {
        "schema",
        "status",
        "scope",
        "provenance",
        "evidence",
        "cell_count",
        "calibration",
        "boundary_layer",
        "meshing",
        "quality",
        "resource",
        "post_mesh_repair",
    }
    if set(document) != expected_root:
        raise CoarseMeshError("coarse mesh config root is incomplete")
    if document.get("schema") != _SCHEMA or document.get("status") != "CONFIGURED":
        raise CoarseMeshError("coarse mesh schema/status is invalid")

    config_root = (root / "config").resolve(strict=True)
    paths = {
        "project_sha256": config_root / "project.toml",
        "cases_sha256": config_root / "cases.csv",
        "markers_sha256": config_root / "markers.toml",
        "topology_smoke_sha256": config_root / "topology_smoke.toml",
        "boundary_layer_trial_sha256": config_root / "boundary_layer_trial.toml",
        "boundary_layer_smoke_sha256": config_root / "boundary_layer_smoke.toml",
        "rear_outlet_freeze_sha256": config_root / "rear_outlet_freeze.toml",
        "source_step_sha256": root / "geometry" / "raw" / "model1.step",
        "pipeline_brep_sha256": root
        / "geometry"
        / "derived"
        / "model1_shared_topology"
        / "model1_shared_topology.brep",
    }
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != set(paths):
        raise CoarseMeshError("coarse mesh provenance is incomplete")
    input_records: dict[str, dict[str, Any]] = {}
    record_names = {
        "project_sha256": "project",
        "cases_sha256": "cases",
        "markers_sha256": "markers",
        "topology_smoke_sha256": "topology_smoke",
        "pipeline_brep_sha256": "pipeline_geometry",
    }
    normalized_hashes: dict[str, str] = {}
    for field, path in paths.items():
        if not path.is_file():
            raise CoarseMeshError(f"{field} source file is missing")
        digest = _sha256(path)
        configured = str(provenance.get(field, "")).casefold()
        if configured != digest:
            raise CoarseMeshError(f"coarse mesh provenance {field} is stale")
        normalized_hashes[field] = digest
        if field in record_names:
            input_records[record_names[field]] = {
                "path": str(path),
                "sha256": digest,
            }
    for field in ("source_step_sha256", "pipeline_brep_sha256"):
        if not _is_read_only(paths[field]):
            raise CoarseMeshError(f"{field} input must remain read-only")

    project = _toml(paths["project_sha256"], "project.toml")
    markers = _toml(paths["markers_sha256"], "markers.toml")
    topology = _toml(paths["topology_smoke_sha256"], "topology_smoke.toml")
    smoke_document = _toml(
        paths["boundary_layer_smoke_sha256"], "boundary_layer_smoke.toml"
    )
    smoke_document.pop("schedule", None)
    try:
        smoke_normalized = normalize_boundary_layer_smoke_config(
            smoke_document,
            project=project,
            marker_config=markers,
            input_records=input_records,
        )
    except BoundaryLayerSmokeError as error:
        raise CoarseMeshError(f"authoritative smoke contract is invalid: {error}") from error

    scope = document.get("scope")
    expected_scope = {
        "case_id",
        "mesh_level",
        "calibration_only_until_resource_gate_passes",
        "production_mesh_candidate",
        "run_su2",
        "run_paraview",
        "nproc",
        "gpu_enabled",
    }
    if not isinstance(scope, Mapping) or set(scope) != expected_scope:
        raise CoarseMeshError("coarse mesh scope is incomplete")
    case_id = str(scope.get("case_id", "")).strip()
    production = project.get("production")
    if (
        scope.get("mesh_level") != "coarse"
        or scope.get("calibration_only_until_resource_gate_passes") is not True
        or scope.get("production_mesh_candidate") is not True
        or scope.get("run_su2") is not False
        or scope.get("run_paraview") is not False
        or scope.get("nproc") != 1
        or scope.get("gpu_enabled") is not False
        or not isinstance(production, Mapping)
        or case_id != str(production.get("design_case", ""))
    ):
        raise CoarseMeshError("coarse mesh scope is unsafe")
    selected_case = _load_case(paths["cases_sha256"], case_id)

    evidence = document.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != {
        "authoritative_boundary_layer_mesh",
        "solution_yplus",
        "rear_outlet_freeze",
    }:
        raise CoarseMeshError("coarse mesh evidence is incomplete")
    mesh_path, mesh_evidence = _evidence_file(
        root,
        config_root,
        evidence["authoritative_boundary_layer_mesh"],
        "authoritative boundary-layer mesh",
    )
    yplus_record = evidence["solution_yplus"]
    yplus_path, yplus_evidence = _evidence_file(
        root, config_root, yplus_record, "solution y+"
    )
    freeze_path, freeze_evidence = _evidence_file(
        root, config_root, evidence["rear_outlet_freeze"], "rear outlet freeze"
    )
    if (
        mesh_evidence.get("status") != "PASS"
        or mesh_evidence.get("diagnostic_quality_status") != "PASS"
        or mesh_evidence.get("source_unchanged") is not True
        or mesh_evidence.get("requires_quality_improvement_before_production")
        is not False
    ):
        raise CoarseMeshError("authoritative boundary-layer mesh is not a quality PASS")
    yplus_payload = yplus_evidence.get("yplus")
    if (
        yplus_evidence.get("status") != "PASS"
        or not isinstance(yplus_payload, Mapping)
        or yplus_payload.get("status") != "PASS"
    ):
        raise CoarseMeshError("solution y+ evidence is not PASS")
    configured_yplus = _finite(
        yplus_record.get("maximum_yplus"), "configured maximum y+", positive=True
    )
    actual_yplus = _finite(
        yplus_payload.get("maximum"), "evidence maximum y+", positive=True
    )
    if not math.isclose(configured_yplus, actual_yplus, rel_tol=1.0e-12, abs_tol=0.0):
        raise CoarseMeshError("configured maximum y+ differs from hash-bound evidence")
    first_height = _finite(
        yplus_record.get("first_layer_height_m"),
        "configured first-layer height",
        positive=True,
    )
    if not math.isclose(
        first_height,
        float(smoke_normalized["first_layer_height_m"]),
        rel_tol=1.0e-12,
        abs_tol=0.0,
    ):
        raise CoarseMeshError("first-layer height differs from the authoritative mesh")
    mesh_su2 = mesh_evidence.get("outputs", {}).get("mesh.su2", {})
    diagnostic_mesh = yplus_evidence.get("outlets_and_mass_balance", {}).get(
        "mesh", {}
    )
    if (
        not isinstance(mesh_su2, Mapping)
        or not isinstance(diagnostic_mesh, Mapping)
        or mesh_su2.get("sha256") != diagnostic_mesh.get("sha256")
    ):
        raise CoarseMeshError("solution y+ evidence uses a different mesh")
    if (
        freeze_evidence.get("status") != "PASS"
        or freeze_evidence.get("boundary_mode_frozen") is not True
        or freeze_evidence.get("production_eligible") is not False
        or freeze_evidence.get("case_id") not in (None, case_id)
    ):
        raise CoarseMeshError("rear-outlet freeze evidence is not design-case PASS")

    cell_count = document.get("cell_count")
    expected_cell = {
        "target_source",
        "relative_tolerance",
        "candidate_size_selection",
        "candidate_characteristic_length_min_m",
        "candidate_characteristic_length_max_m",
    }
    project_mesh = project.get("mesh")
    grid_levels = project_mesh.get("grid_levels") if isinstance(project_mesh, Mapping) else None
    if (
        not isinstance(cell_count, Mapping)
        or set(cell_count) != expected_cell
        or not isinstance(grid_levels, Mapping)
        or cell_count.get("target_source")
        != "project.mesh.grid_levels.coarse_target_cells"
        or cell_count.get("candidate_size_selection")
        != "two_or_more_pass_calibrations_power_law"
    ):
        raise CoarseMeshError("coarse cell-count contract is incomplete")
    target = _positive_int(grid_levels.get("coarse_target_cells"), "coarse target")
    tolerance = _finite(
        cell_count.get("relative_tolerance"), "relative cell-count tolerance", positive=True
    )
    size_min = _finite(
        cell_count.get("candidate_characteristic_length_min_m"),
        "minimum characteristic length",
        positive=True,
    )
    size_max = _finite(
        cell_count.get("candidate_characteristic_length_max_m"),
        "maximum characteristic length",
        positive=True,
    )
    if tolerance > 0.10 or size_min >= size_max:
        raise CoarseMeshError("coarse cell-count tolerance or size bounds are unsafe")
    lower = math.ceil(target * (1.0 - tolerance))
    upper = math.floor(target * (1.0 + tolerance))

    calibration = document.get("calibration")
    expected_calibration = {
        "characteristic_lengths_m",
        "maximum_3d_elements",
        "maximum_elapsed_seconds",
        "new_output_directory_required",
    }
    if not isinstance(calibration, Mapping) or set(calibration) != expected_calibration:
        raise CoarseMeshError("coarse calibration contract is incomplete")
    raw_sizes = calibration.get("characteristic_lengths_m")
    if not isinstance(raw_sizes, list):
        raise CoarseMeshError("coarse calibration sizes must be a list")
    calibration_sizes = [
        _finite(value, "calibration characteristic length", positive=True)
        for value in raw_sizes
    ]
    calibration_cap = _positive_int(
        calibration.get("maximum_3d_elements"), "calibration element cap"
    )
    calibration_timeout = _finite(
        calibration.get("maximum_elapsed_seconds"),
        "calibration timeout",
        positive=True,
    )
    if (
        len(calibration_sizes) < MINIMUM_CALIBRATION_SAMPLES
        or len(calibration_sizes) != len(set(calibration_sizes))
        or any(not size_min <= value <= size_max for value in calibration_sizes)
        or calibration_cap >= lower
        or calibration_timeout > 840.0
        or calibration.get("new_output_directory_required") is not True
    ):
        raise CoarseMeshError("coarse calibration policy is unsafe")

    boundary = document.get("boundary_layer")
    expected_boundary = {
        "first_layer_height_source",
        "construction_shell_height_source",
        "growth_ratio_source",
        "minimum_layer_count_source",
        "target_layer_count_source",
        "method",
        "thickness_safety_factor",
        "maximum_layer_count",
        "maximum_core_to_last_layer_ratio",
        "maximum_clearance_fraction",
        "planning_core_size_m",
        "requires_solution_based_yplus_validation",
        "requires_solution_based_outer_edge_validation",
    }
    if not isinstance(boundary, Mapping) or set(boundary) != expected_boundary:
        raise CoarseMeshError("coarse boundary-layer contract is incomplete")
    if (
        boundary.get("first_layer_height_source")
        != "evidence.solution_yplus.first_layer_height_m"
        or boundary.get("construction_shell_height_source")
        != "base_smoke_contract.construction_first_layer_height_m"
        or boundary.get("growth_ratio_source")
        != "project.mesh.initial_growth_ratio"
        or boundary.get("minimum_layer_count_source")
        != "project.mesh.minimum_prism_layers"
        or boundary.get("target_layer_count_source")
        != "project.mesh.target_prism_layers"
        or boundary.get("requires_solution_based_yplus_validation") is not True
        or boundary.get("requires_solution_based_outer_edge_validation") is not True
    ):
        raise CoarseMeshError("coarse boundary-layer sources are unsafe")
    try:
        maximum_clearance_fraction = _finite(
            boundary.get("maximum_clearance_fraction"),
            "maximum clearance fraction",
            positive=True,
        )
        if maximum_clearance_fraction >= 0.5:
            raise CoarseMeshError(
                "maximum clearance fraction must be below one half"
            )
        boundary_design = design_boundary_layer_stack(
            project=project,
            selected_case=selected_case,
            first_layer_height_m=first_height,
            estimated_yplus=actual_yplus,
            core_size_m=_finite(
                boundary.get("planning_core_size_m"),
                "planning core size",
                positive=True,
            ),
            policy={
                "method": boundary.get("method"),
                "thickness_safety_factor": boundary.get(
                    "thickness_safety_factor"
                ),
                "maximum_layer_count": boundary.get("maximum_layer_count"),
                "maximum_core_to_last_layer_ratio": boundary.get(
                    "maximum_core_to_last_layer_ratio"
                ),
            },
        )
    except BoundaryLayerDesignError as error:
        raise CoarseMeshError(f"coarse boundary-layer design failed: {error}") from error

    repair = document.get("post_mesh_repair")
    smoke_repair = smoke_normalized["post_mesh_repair"]
    if not isinstance(repair, Mapping) or set(repair) != set(smoke_repair):
        raise CoarseMeshError("coarse post-mesh repair contract is incomplete")
    layer_dependent = "expected_initial_below_threshold_prism_count"
    for key in smoke_repair:
        if key == layer_dependent:
            continue
        if repair.get(key) != smoke_repair[key]:
            raise CoarseMeshError(f"coarse post-mesh repair {key} is stale")
    expected_layer_dependent = (
        int(smoke_repair[layer_dependent])
        * int(boundary_design["layer_count"])
        // int(smoke_normalized["layer_count"])
    )
    if (
        int(smoke_repair[layer_dependent])
        * int(boundary_design["layer_count"])
        % int(smoke_normalized["layer_count"])
        != 0
        or repair.get(layer_dependent) != expected_layer_dependent
    ):
        raise CoarseMeshError("layer-dependent repair count is inconsistent")

    meshing = document.get("meshing")
    expected_meshing = {
        "element_order",
        "core_tetra_meshing_algorithm",
        "core_tetra_meshing_algorithm_name",
        "general_num_threads",
        "allowed_3d_element_types",
        "runtime_tags_present",
        "additional_refinement_groups",
    }
    if (
        not isinstance(meshing, Mapping)
        or set(meshing) != expected_meshing
        or meshing.get("element_order") != 1
        or meshing.get("core_tetra_meshing_algorithm") != 10
        or meshing.get("core_tetra_meshing_algorithm_name") != "HXT"
        or meshing.get("general_num_threads") != 1
        or meshing.get("allowed_3d_element_types")
        != ["Prism 6", "Pyramid 5", "Tetrahedron 4"]
        or meshing.get("runtime_tags_present") is not False
    ):
        raise CoarseMeshError("coarse meshing policy is unsafe")

    raw_coarse_groups = meshing.get("additional_refinement_groups")
    if not isinstance(raw_coarse_groups, list) or not raw_coarse_groups:
        raise CoarseMeshError(
            "coarse additional refinement groups are missing"
        )
    expected_group_fields = {
        "name",
        "surface_fingerprint_ids",
        "minimum_size_m",
        "distance_max_m",
        "sampling",
    }
    wall_fingerprint_set = {
        str(value).casefold()
        for value in smoke_normalized["wall_surface_fingerprints"]
    }
    base_quality_improvement = smoke_normalized.get("quality_improvement")
    base_groups = (
        base_quality_improvement.get("additional_refinement_groups")
        if isinstance(base_quality_improvement, Mapping)
        else None
    )
    if not isinstance(base_groups, list):
        raise CoarseMeshError(
            "authoritative smoke refinement groups are missing"
        )
    base_group_fingerprints = {
        str(fingerprint).casefold()
        for group in base_groups
        if isinstance(group, Mapping)
        for fingerprint in group.get("surface_fingerprint_ids", [])
    }
    normalized_coarse_groups: list[dict[str, Any]] = []
    coarse_group_names: set[str] = set()
    coarse_group_fingerprints: set[str] = set()
    for index, raw_group in enumerate(raw_coarse_groups):
        if (
            not isinstance(raw_group, Mapping)
            or set(raw_group) != expected_group_fields
        ):
            raise CoarseMeshError(
                f"coarse additional refinement group {index} is incomplete"
            )
        name = str(raw_group.get("name", "")).strip()
        raw_fingerprints = raw_group.get("surface_fingerprint_ids")
        if (
            _REFINEMENT_GROUP_NAME.fullmatch(name) is None
            or name in coarse_group_names
            or not isinstance(raw_fingerprints, list)
        ):
            raise CoarseMeshError(
                f"coarse additional refinement group {index} name is invalid"
            )
        fingerprints = [str(value).casefold() for value in raw_fingerprints]
        if (
            len(fingerprints) != 6
            or len(fingerprints) != len(set(fingerprints))
            or any(_SHA256.fullmatch(value) is None for value in fingerprints)
            or not set(fingerprints) <= wall_fingerprint_set
            or set(fingerprints) & coarse_group_fingerprints
            or set(fingerprints) & base_group_fingerprints
        ):
            raise CoarseMeshError(
                f"coarse additional refinement group {index} fingerprints are invalid"
            )
        minimum_size = _finite(
            raw_group.get("minimum_size_m"),
            f"coarse additional refinement group {index} minimum size",
            positive=True,
        )
        distance_max = _finite(
            raw_group.get("distance_max_m"),
            f"coarse additional refinement group {index} distance maximum",
            positive=True,
        )
        sampling = _positive_int(
            raw_group.get("sampling"),
            f"coarse additional refinement group {index} sampling",
        )
        if (
            minimum_size > min(calibration_sizes)
            or distance_max > min(calibration_sizes)
            or sampling > 10_000
        ):
            raise CoarseMeshError(
                f"coarse additional refinement group {index} numeric policy is unsafe"
            )
        coarse_group_names.add(name)
        coarse_group_fingerprints.update(fingerprints)
        normalized_coarse_groups.append(
            {
                "name": name,
                "surface_fingerprint_ids": sorted(fingerprints),
                "minimum_size_m": minimum_size,
                "distance_max_m": distance_max,
                "sampling": sampling,
            }
        )
    if coarse_group_names != _COARSE_REFINEMENT_GROUP_NAMES:
        raise CoarseMeshError(
            "coarse additional refinement group names are not the approved recovery set"
        )

    quality = document.get("quality")
    expected_quality = {
        "minimum_core_tetra_gamma",
        "minimum_prism_scaled_jacobian",
        "maximum_nonpositive_volume_count",
        "maximum_nonpositive_jacobian_count",
        "maximum_nonfinite_count",
        "maximum_core_tetra_below_gamma_count",
        "maximum_prism_below_threshold_element_count",
        "maximum_prism_below_threshold_column_count",
        "maximum_nonorthogonality_degrees",
        "maximum_above_70_degrees_fraction",
        "required_wall_fingerprint_coverage",
    }
    if not isinstance(quality, Mapping) or set(quality) != expected_quality:
        raise CoarseMeshError("coarse quality contract is incomplete")
    if (
        quality.get("minimum_core_tetra_gamma")
        != smoke_normalized["quality_improvement"]["minimum_core_tetra_gamma"]
        or quality.get("minimum_prism_scaled_jacobian")
        != smoke_normalized["quality_improvement"][
            "minimum_prism_scaled_jacobian"
        ]
        or any(
            quality.get(key) != 0
            for key in (
                "maximum_nonpositive_volume_count",
                "maximum_nonpositive_jacobian_count",
                "maximum_nonfinite_count",
                "maximum_core_tetra_below_gamma_count",
                "maximum_prism_below_threshold_element_count",
                "maximum_prism_below_threshold_column_count",
            )
        )
        or not 0.0
        < _finite(
            quality.get("maximum_nonorthogonality_degrees"),
            "maximum nonorthogonality",
            positive=True,
        )
        < 90.0
        or not 0.0
        <= _finite(
            quality.get("maximum_above_70_degrees_fraction"),
            "high nonorthogonality fraction",
        )
        <= 0.05
        or quality.get("required_wall_fingerprint_coverage") != 1.0
    ):
        raise CoarseMeshError("coarse quality thresholds are unsafe")

    resource = document.get("resource")
    expected_resource = {
        "minimum_calibration_samples",
        "safety_factor",
        "os_physical_memory_reserve_bytes",
        "minimum_disk_free_bytes",
        "physical_memory_required",
        "pagefile_cannot_rescue_physical_memory_failure",
        "projection_worker_memory_limit_bytes",
        "calibration_worker_memory_limit_bytes",
        "worker_monitor_margin_bytes",
    }
    if (
        not isinstance(resource, Mapping)
        or set(resource) != expected_resource
        or resource.get("minimum_calibration_samples")
        != MINIMUM_CALIBRATION_SAMPLES
        or resource.get("safety_factor") != SAFETY_FACTOR
        or resource.get("os_physical_memory_reserve_bytes") != OS_RESERVE_BYTES
        or _positive_int(resource.get("minimum_disk_free_bytes"), "minimum disk")
        < 20 * 1024**3
        or resource.get("physical_memory_required") is not True
        or resource.get("pagefile_cannot_rescue_physical_memory_failure") is not True
        or not 512 * 1024**2
        <= _positive_int(
            resource.get("projection_worker_memory_limit_bytes"),
            "projection worker memory limit",
        )
        <= 4 * 1024**3
        or not _positive_int(
            resource.get("projection_worker_memory_limit_bytes"),
            "projection worker memory limit",
        )
        <= _positive_int(
            resource.get("calibration_worker_memory_limit_bytes"),
            "calibration worker memory limit",
        )
        <= 8 * 1024**3
        or _positive_int(
            resource.get("worker_monitor_margin_bytes"),
            "worker monitor margin",
        )
        < 256 * 1024**2
    ):
        raise CoarseMeshError("coarse resource contract is unsafe")

    normalized: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "CONFIGURED",
        "case_id": case_id,
        "mesh_level": "coarse",
        "production_mesh_candidate": True,
        "calibration_only_until_resource_gate_passes": True,
        "run_su2": False,
        "run_paraview": False,
        "nproc": 1,
        "gpu_enabled": False,
        "provenance": normalized_hashes,
        "source_step_path": str(paths["source_step_sha256"]),
        "pipeline_brep_path": str(paths["pipeline_brep_sha256"]),
        "evidence": {
            "authoritative_boundary_layer_mesh": {
                "path": str(mesh_path),
                "sha256": _sha256(mesh_path),
            },
            "solution_yplus": {
                "path": str(yplus_path),
                "sha256": _sha256(yplus_path),
                "maximum_yplus": actual_yplus,
                "first_layer_height_m": first_height,
            },
            "rear_outlet_freeze": {
                "path": str(freeze_path),
                "sha256": _sha256(freeze_path),
            },
        },
        "selected_case": selected_case,
        "target_3d_elements": target,
        "relative_count_tolerance": tolerance,
        "target_element_range": [lower, upper],
        "candidate_characteristic_length_bounds_m": [size_min, size_max],
        "calibration_characteristic_lengths_m": calibration_sizes,
        "calibration_maximum_3d_elements": calibration_cap,
        "calibration_maximum_elapsed_seconds": calibration_timeout,
        "boundary_layer_design": boundary_design,
        "solver_marker_names": list(smoke_normalized["solver_marker_names"]),
        "wall_surface_fingerprints": list(
            smoke_normalized["wall_surface_fingerprints"]
        ),
        "coarse_additional_refinement_groups": sorted(
            normalized_coarse_groups, key=lambda value: value["name"]
        ),
        "quality": dict(quality),
        "resource": dict(resource),
        "post_mesh_repair": copy.deepcopy(dict(repair)),
        "base_smoke_contract": smoke_normalized,
    }
    marker_geometry = markers.get("pipeline_geometry")
    marker_matching = markers.get("matching")
    if not isinstance(marker_geometry, Mapping) or not isinstance(
        marker_matching, Mapping
    ):
        raise CoarseMeshError("marker clearance inputs are incomplete")
    clearance_evidence_contract = _make_clearance_evidence_contract(
        pipeline_brep_path=paths["pipeline_brep_sha256"],
        pipeline_brep_sha256=normalized_hashes["pipeline_brep_sha256"],
        markers_path=paths["markers_sha256"],
        markers_sha256=normalized_hashes["markers_sha256"],
        marker_pipeline_geometry_sha256=str(
            marker_geometry.get("sha256", "")
        ).casefold(),
        coordinate_absolute_tolerance_m=_finite(
            marker_matching.get("coordinate_absolute_tolerance_m"),
            "marker coordinate tolerance",
            positive=True,
        ),
        required_total_thickness_m=float(boundary_design["total_thickness_m"]),
        wall_physical_name=str(smoke_normalized["wall_physical_name"]),
        wall_surface_fingerprints=list(
            smoke_normalized["wall_surface_fingerprints"]
        ),
        direction_probe_distance_m=float(
            smoke_normalized["direction_probe_distance_m"]
        ),
    )
    normalized["clearance_evidence_contract"] = clearance_evidence_contract
    normalized["clearance_evidence_contract_sha256"] = _canonical_hash(
        clearance_evidence_contract
    )
    normalized["boundary_layer_maximum_clearance_fraction"] = (
        maximum_clearance_fraction
    )
    normalized["normalized_config_sha256"] = _canonical_hash(normalized)
    return normalized


def make_coarse_strategy_config(
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    *,
    local_schedule_binding: Mapping[str, Any] | None = None,
    projection_only: bool = False,
) -> dict[str, Any]:
    """Derive the existing geometry strategy inputs for one bounded calibration."""

    configured_hash = contract.get("normalized_config_sha256")
    unsigned = dict(contract)
    unsigned.pop("normalized_config_sha256", None)
    if (
        contract.get("schema") != _SCHEMA
        or not isinstance(configured_hash, str)
        or _canonical_hash(unsigned) != configured_hash
    ):
        raise CoarseMeshError("normalized coarse mesh contract hash is stale")
    characteristic = _finite(
        characteristic_length_m, "calibration characteristic length", positive=True
    )
    allowed = [
        float(value)
        for value in contract.get("calibration_characteristic_lengths_m", [])
    ]
    if not any(
        math.isclose(characteristic, value, rel_tol=1.0e-12, abs_tol=0.0)
        for value in allowed
    ):
        raise CoarseMeshError("calibration characteristic length is not authorized")
    base = copy.deepcopy(contract.get("base_smoke_contract"))
    if not isinstance(base, dict):
        raise CoarseMeshError("coarse contract has no authoritative smoke base")
    base.pop("normalized_config_sha256", None)
    quality_improvement = base.get("quality_improvement")
    coarse_groups = contract.get("coarse_additional_refinement_groups")
    if (
        not isinstance(quality_improvement, dict)
        or not isinstance(quality_improvement.get("additional_refinement_groups"), list)
        or not isinstance(coarse_groups, list)
        or not coarse_groups
    ):
        raise CoarseMeshError(
            "coarse strategy refinement groups are incomplete"
        )
    combined_groups = [
        *copy.deepcopy(quality_improvement["additional_refinement_groups"]),
        *copy.deepcopy(coarse_groups),
    ]
    combined_names = [str(group.get("name", "")) for group in combined_groups]
    combined_fingerprints = [
        str(fingerprint).casefold()
        for group in combined_groups
        if isinstance(group, Mapping)
        for fingerprint in group.get("surface_fingerprint_ids", [])
    ]
    if (
        len(combined_names) != len(set(combined_names))
        or len(combined_fingerprints) != len(set(combined_fingerprints))
    ):
        raise CoarseMeshError(
            "coarse strategy refinement groups overlap or repeat"
        )
    quality_improvement["additional_refinement_groups"] = combined_groups
    design = contract.get("boundary_layer_design")
    if not isinstance(design, Mapping) or design.get("status") != "PASS":
        raise CoarseMeshError("coarse boundary-layer design is not PASS")
    if projection_only:
        if local_schedule_binding is not None:
            raise CoarseMeshError(
                "projection-only strategy must not consume a subdivision schedule"
            )
        validated_binding = None
    else:
        if local_schedule_binding is None:
            raise CoarseMeshError(
                "coarse calibration requires an explicit hash-bound local schedule"
            )
        try:
            validated_binding = validate_boundary_layer_schedule_binding(
                contract, local_schedule_binding
            )
        except BoundaryLayerScheduleBindingError as error:
            raise CoarseMeshError(
                f"coarse local boundary-layer schedule is invalid: {error}"
            ) from error
    base.update(
        {
            "schema": _SCHEMA,
            "status": "CONFIGURED",
            "smoke_only": False,
            "production_mesh_eligible": False,
            "calibration_only": True,
            "repair_audit_only": False,
            "projection_only": bool(projection_only),
            "contract_mode": "coarse_calibration",
            "max_3d_elements": int(contract["calibration_maximum_3d_elements"]),
            "layer_count": int(design["layer_count"]),
            "growth_ratio": float(design["growth_ratio"]),
            "first_layer_height_m": float(design["first_layer_height_m"]),
            "construction_first_layer_height_m": float(
                base["construction_first_layer_height_m"]
            ),
            "characteristic_length_m": characteristic,
            "post_mesh_repair": copy.deepcopy(contract["post_mesh_repair"]),
        }
    )
    if validated_binding is not None:
        base["coarse_contract_sha256"] = str(
            contract["normalized_config_sha256"]
        )
        base["local_boundary_layer_schedule"] = copy.deepcopy(
            validated_binding
        )
    base["normalized_config_sha256"] = _canonical_hash(base)
    return base


def _peak_working_set_bytes() -> int | None:
    """Return this worker's peak resident set using only the standard library."""

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            if not psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return None
            return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak if os.uname().sysname == "Darwin" else peak * 1024
    except (AttributeError, ImportError, OSError, ValueError):
        return None


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _write_new_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError as error:
        raise CoarseMeshError(f"cannot inspect output path links: {error}") from error


def _reject_existing_link_ancestors(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and _is_link_like(candidate):
            raise CoarseMeshError(f"{label} contains a symbolic link or junction")


def _prepare_new_output(
    output_directory: str | os.PathLike[str],
    allowed_output_root: str | os.PathLike[str],
) -> tuple[Path, Path]:
    raw_root = Path(allowed_output_root).expanduser()
    raw_output = Path(output_directory).expanduser()
    if ".." in raw_root.parts or ".." in raw_output.parts:
        raise CoarseMeshError("coarse calibration output paths must not contain '..'")

    lexical_root = Path(os.path.abspath(raw_root))
    lexical_output = Path(os.path.abspath(raw_output))
    _reject_existing_link_ancestors(lexical_root, "allowed output root")
    if lexical_root.exists() and not lexical_root.is_dir():
        raise CoarseMeshError("allowed output root is not a directory")
    lexical_root.mkdir(parents=True, exist_ok=True)
    _reject_existing_link_ancestors(lexical_root, "allowed output root")
    resolved_root = lexical_root.resolve(strict=True)

    _reject_existing_link_ancestors(lexical_output, "coarse calibration output")
    resolved_output = lexical_output.resolve(strict=False)
    if resolved_output == resolved_root:
        raise CoarseMeshError("coarse calibration output must be a strict child")
    if resolved_root not in resolved_output.parents:
        raise CoarseMeshError(
            "coarse calibration output is outside the allowed output root"
        )
    if lexical_output.exists() or resolved_output.exists():
        raise CoarseMeshError("coarse calibration output must not already exist")

    lexical_output.parent.mkdir(parents=True, exist_ok=True)
    _reject_existing_link_ancestors(lexical_output.parent, "coarse calibration output")
    resolved_parent = lexical_output.parent.resolve(strict=True)
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise CoarseMeshError(
            "coarse calibration output parent escaped the allowed output root"
        )
    resolved_output = resolved_parent / lexical_output.name
    if resolved_output.exists() or _is_link_like(resolved_output):
        raise CoarseMeshError("coarse calibration output must not already exist")
    return resolved_output, resolved_root


def _retained_artifacts(
    staging_paths: Mapping[str, Path], *, output_directory: Path
) -> dict[str, dict[str, Any]]:
    retained: dict[str, dict[str, Any]] = {}
    for name, staging_path in sorted(staging_paths.items()):
        if not staging_path.is_file():
            continue
        retained[name] = {
            "path": str(output_directory / name),
            "sha256": _sha256(staging_path),
            "size_bytes": staging_path.stat().st_size,
            "validated": False,
        }
    return retained


def build_coarse_calibration(
    *,
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    output_directory: str | os.PathLike[str],
    allowed_output_root: str | os.PathLike[str],
    strategy: RealProjectBoundaryLayerStrategy,
    local_schedule_binding: Mapping[str, Any] | None = None,
    projection_evidence: Mapping[str, Any] | None = None,
    gmsh_module: Any | None = None,
    controller_argv: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Generate one hash-bound calibration mesh in two fresh Gmsh sessions.

    This remains a calibration artifact and never authorizes SU2.  Its output is
    suitable for the resource projection only after generation, fresh readback,
    hard quality gates and the independent streaming SU2 audit all pass.
    """

    configured_lengths = contract.get("calibration_characteristic_lengths_m")
    if not isinstance(configured_lengths, list) or not configured_lengths:
        raise CoarseMeshError(
            "coarse calibration contract has no configured calibration points"
        )
    first_characteristic = _finite(
        configured_lengths[0],
        "first configured calibration characteristic length",
        positive=True,
    )
    if not isinstance(projection_evidence, Mapping):
        raise CoarseMeshError(
            "coarse calibration requires explicit projection evidence"
        )
    try:
        projected_count = _positive_int(
            projection_evidence.get("projected_3d_elements"),
            "projected 3D element count",
        )
        validated_projection = validate_projection_evidence_record(
            projection_evidence,
            expected_path=str(projection_evidence.get("path", "")),
            expected_sha256=str(projection_evidence.get("sha256", "")),
            expected_config_sha256=str(
                projection_evidence.get("config_sha256", "")
            ),
            expected_coarse_contract_sha256=str(
                contract["normalized_config_sha256"]
            ),
            expected_characteristic_length_m=first_characteristic,
            expected_projected_3d_elements=projected_count,
        )
    except (CoarseProjectionEvidenceError, KeyError, CoarseMeshError) as error:
        raise CoarseMeshError(
            f"coarse projection evidence is invalid: {error}"
        ) from error
    is_first_point = math.isclose(
        float(characteristic_length_m),
        first_characteristic,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    )

    strategy_config = make_coarse_strategy_config(
        contract,
        characteristic_length_m,
        local_schedule_binding=local_schedule_binding,
    )
    source = Path(str(contract["pipeline_brep_path"])).resolve(strict=True)
    if (
        not source.is_file()
        or source.suffix.casefold() != ".brep"
        or not _is_read_only(source)
        or _sha256(source)
        != contract.get("provenance", {}).get("pipeline_brep_sha256")
    ):
        raise CoarseMeshError("coarse calibration source BREP is invalid")
    output, allowed_root = _prepare_new_output(
        output_directory, allowed_output_root
    )
    staging = output.parent / f".coarse-calibration-{os.getpid()}-{uuid4().hex}"
    staging.mkdir()
    started = _utc_now()
    monotonic_started = time.monotonic()
    source_before = {
        "path": str(source),
        "sha256": _sha256(source),
        "size_bytes": source.stat().st_size,
        "read_only": _is_read_only(source),
    }
    manifest: dict[str, Any] = {
        "schema": "cfdpipe.coarse_mesh_calibration_manifest.v1",
        "status": "FAIL",
        "calibration_only": True,
        "production_mesh_eligible": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "external_stderr": None,
        "started_at_utc": started,
        "ended_at_utc": None,
        "elapsed_seconds": None,
        "peak_working_set_bytes": None,
        "output_directory": str(output),
        "allowed_output_root": str(allowed_root),
        "controller_argv": list(controller_argv),
        "coarse_contract_sha256": contract["normalized_config_sha256"],
        "strategy_config": strategy_config,
        "strategy_config_sha256": strategy_config["normalized_config_sha256"],
        "characteristic_length_m": float(characteristic_length_m),
        "projection_evidence": copy.deepcopy(validated_projection),
        "projection_generation_count_crosscheck": {
            "status": "PENDING" if is_first_point else "NOT_APPLICABLE",
            "required_for_first_configured_point": is_first_point,
            "first_configured_characteristic_length_m": first_characteristic,
            "selected_characteristic_length_m": float(characteristic_length_m),
            "projected_3d_elements": projected_count,
            "generated_3d_elements": None,
            "exact_match": None,
            "second_point_uses_first_projection_lineage_only": not is_first_point,
        },
        "source_before": source_before,
        "source_after": None,
        "source_unchanged": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_sessions": {
            "build": {
                "finalize_called": False,
                "logger_messages": [],
                "cleanup_errors": [],
            },
            "readback": {
                "finalize_called": False,
                "logger_messages": [],
                "cleanup_errors": [],
            },
        },
        "strategy_evidence": {"build": {}, "readback": {}},
        "generation_audit": None,
        "readback_audit": None,
        "su2_validation": None,
        "outputs": {},
        "retained_artifacts": {},
        "deferred_diagnostic_finalization": None,
        "resource_sample_eligible": False,
        "resource_sample_eligibility": None,
        "error": None,
    }
    log_lines = [f"[{started}] coarse mesh calibration started"]
    primary: BaseException | None = None
    rendered = ""
    msh_path = staging / "mesh.msh"
    su2_path = staging / "mesh.su2"
    try:
        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_path = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_path).resolve()) if module_path else "unknown"
        )

        def build_action() -> dict[str, Any]:
            gmsh.model.add("cfdpipe_coarse_mesh_calibration")
            payload = strategy.build(gmsh, source, strategy_config, staging)
            audit = _call_audit(payload, strategy_config)
            gmsh.write(str(msh_path))
            gmsh.write(str(su2_path))
            return audit

        generation = _session(gmsh, build_action, manifest, "build")
        manifest["generation_audit"] = generation
        generated_count = _positive_int(
            generation.get("element_count_3d"),
            "generated calibration 3D element count",
        )
        crosscheck = manifest["projection_generation_count_crosscheck"]
        crosscheck["generated_3d_elements"] = generated_count
        if is_first_point:
            crosscheck["exact_match"] = generated_count == projected_count
            crosscheck["status"] = (
                "PASS" if crosscheck["exact_match"] else "FAIL"
            )
            if not crosscheck["exact_match"]:
                raise CoarseMeshError(
                    "first calibration generated count differs from the "
                    "hash-bound projection"
                )
        else:
            crosscheck["exact_match"] = None
        manifest["strategy_evidence"]["build"] = _strategy_evidence(
            strategy, "build"
        )
        _reject_fatal_gmsh_log(manifest, "build", defer_ill_shaped=True)
        if not msh_path.is_file() or msh_path.stat().st_size <= 0:
            raise CoarseMeshError("Gmsh did not write a nonempty calibration MSH")
        serialized = audit_su2_mesh(
            su2_path,
            target_element_range=(1, int(strategy_config["max_3d_elements"])),
            expected_markers=contract["solver_marker_names"],
        )
        manifest["su2_validation"] = serialized
        if serialized.get("status") != "PASS":
            raise CoarseMeshError(
                "independent streaming SU2 calibration audit failed: "
                + json.dumps(
                    serialized.get("error"),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
        if serialized.get("nelem") != generation.get("element_count_3d"):
            raise CoarseMeshError(
                "serialized SU2 element count does not match Gmsh generation"
            )
        if serialized.get("volume_element_types") != generation.get(
            "element_types_3d"
        ):
            raise CoarseMeshError(
                "serialized SU2 element type counts do not match Gmsh generation"
            )
        if serialized.get("marker_element_counts") != generation.get(
            "marker_face_counts"
        ):
            raise CoarseMeshError(
                "serialized SU2 marker counts do not match Gmsh generation"
            )

        def readback_action() -> dict[str, Any]:
            gmsh.open(str(msh_path))
            payload = strategy.readback(gmsh, msh_path, strategy_config)
            return _call_audit(payload, strategy_config)

        readback = _session(gmsh, readback_action, manifest, "readback")
        manifest["readback_audit"] = readback
        manifest["strategy_evidence"]["readback"] = _strategy_evidence(
            strategy, "readback"
        )
        _reject_fatal_gmsh_log(manifest, "readback")
        if _audit_signature(generation) != _audit_signature(readback):
            raise CoarseMeshError("fresh calibration MSH readback changed topology")
        if _quality_gate_signature(
            manifest["strategy_evidence"]["build"]
        ) != _quality_gate_signature(manifest["strategy_evidence"]["readback"]):
            raise CoarseMeshError("fresh calibration MSH readback changed quality")
        source_after = {
            "path": str(source),
            "sha256": _sha256(source),
            "size_bytes": source.stat().st_size,
            "read_only": _is_read_only(source),
        }
        manifest["source_after"] = source_after
        manifest["source_unchanged"] = source_after == source_before
        if not manifest["source_unchanged"]:
            raise CoarseMeshError("source BREP changed during calibration")
        outputs = {
            "mesh.msh": {
                "path": str(output / "mesh.msh"),
                "sha256": _sha256(msh_path),
                "size_bytes": msh_path.stat().st_size,
            },
            "mesh.su2": {
                "path": str(output / "mesh.su2"),
                "sha256": _sha256(su2_path),
                "size_bytes": su2_path.stat().st_size,
            },
        }
        if serialized.get("sha256") != outputs["mesh.su2"]["sha256"]:
            raise CoarseMeshError(
                "serialized SU2 audit SHA-256 does not match the final output"
            )
        if serialized.get("file_size_bytes") != outputs["mesh.su2"]["size_bytes"]:
            raise CoarseMeshError(
                "serialized SU2 audit size does not match the final output"
            )
        manifest["outputs"] = outputs
        deferred = _finalize_deferred_gmsh_log_diagnostics(
            manifest, generation, readback, serialized, msh_path, su2_path
        )
        manifest["deferred_diagnostic_finalization"] = deferred
        if (
            not isinstance(deferred, Mapping)
            or deferred.get("status") != "PASS"
            or manifest.get("diagnostic_quality_status") != "PASS"
            or manifest.get("requires_quality_improvement_before_production")
            is not False
        ):
            raise CoarseMeshError(
                "deferred diagnostic finalization did not return strict PASS"
            )
        manifest["status"] = "PASS"
    except BaseException as error:
        primary = error
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        log_lines.append(rendered)
        for phase in ("build", "readback"):
            try:
                evidence = _strategy_evidence(strategy, phase)
                if evidence:
                    manifest["strategy_evidence"][phase] = evidence
            except BaseException as evidence_error:
                manifest["strategy_evidence"][phase] = {
                    "evidence_error": (
                        f"{type(evidence_error).__name__}: {evidence_error}"
                    )
                }
    if manifest["source_after"] is None:
        try:
            manifest["source_after"] = {
                "path": str(source),
                "sha256": _sha256(source),
                "size_bytes": source.stat().st_size,
                "read_only": _is_read_only(source),
            }
            manifest["source_unchanged"] = (
                manifest["source_after"] == source_before
            )
        except BaseException as snapshot_error:
            manifest["source_snapshot_error"] = (
                f"{type(snapshot_error).__name__}: {snapshot_error}"
            )
    manifest["ended_at_utc"] = _utc_now()
    manifest["elapsed_seconds"] = time.monotonic() - monotonic_started
    manifest["peak_working_set_bytes"] = _peak_working_set_bytes()
    elapsed_limit = float(contract["calibration_maximum_elapsed_seconds"])
    peak_value = manifest["peak_working_set_bytes"]
    peak_valid = (
        isinstance(peak_value, int)
        and not isinstance(peak_value, bool)
        and peak_value > 0
    )
    quality_pass = primary is None and manifest.get("status") == "PASS"
    elapsed_value = float(manifest["elapsed_seconds"])
    elapsed_within_limit = (
        math.isfinite(elapsed_value) and 0.0 < elapsed_value <= elapsed_limit
    )
    manifest["resource_sample_eligibility"] = {
        "quality_gate_pass": quality_pass,
        "elapsed_within_limit": elapsed_within_limit,
        "maximum_elapsed_seconds": elapsed_limit,
        "peak_working_set_valid": peak_valid,
    }
    manifest["resource_sample_eligible"] = bool(
        quality_pass and elapsed_within_limit and peak_valid
    )
    for phase in ("build", "readback"):
        for message in manifest["gmsh_sessions"][phase]["logger_messages"]:
            log_lines.append(f"[gmsh:{phase}] {message}")
    if primary is None:
        log_lines.append(f"[{manifest['ended_at_utc']}] status=PASS")
    else:
        manifest["outputs"] = {}
        manifest["retained_artifacts"] = _retained_artifacts(
            {"mesh.msh": msh_path, "mesh.su2": su2_path},
            output_directory=output,
        )
        manifest["error"] = {
            "type": type(primary).__name__,
            "message": str(primary),
            "traceback": rendered,
        }
        log_lines.append(f"[{manifest['ended_at_utc']}] status=FAIL")
    _write_new_text(staging / "gmsh_coarse_calibration.log", "\n".join(log_lines) + "\n")
    _write_new_json(staging / "coarse_calibration_manifest.json", manifest)
    staging.rename(output)
    if primary is not None:
        if isinstance(primary, (KeyboardInterrupt, SystemExit)):
            raise primary
        raise CoarseMeshError(
            f"coarse mesh calibration failed: {manifest['error']['message']}"
        ) from primary
    return manifest


__all__ = [
    "CoarseMeshError",
    "build_coarse_calibration",
    "make_coarse_strategy_config",
    "normalize_coarse_mesh_config",
]
