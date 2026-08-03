"""Bounded serial SST RANS and solution-based y+ quality gate."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tomllib
import traceback
from typing import Any, Mapping, Sequence

from .atmosphere import us_standard_atmosphere_1976
from .bridges.paraview_bridge import ParaViewBridge, ParaViewBridgeError
from .bridges.su2_bridge import (
    SU2Bridge,
    SU2BridgeError,
    discover_su2_rans_reference,
    parse_history_iterations,
    parse_su2_freestream_log,
    render_project_rans_restart_config,
    render_project_rans_smoke_config,
    scan_fatal_output,
    scan_nonfatal_diagnostics,
    select_new_visualization,
    snapshot_visualizations,
    validate_visualization_file,
)
from .outlet_diagnostics import parse_topology_smoke_su2
from .pipeline import load_real_connection_inputs, select_real_case
from .process import CommandExecutionError, CommandResult, CommandRunner
from .toolchain import Toolchain


class RANSSmokePipelineError(RuntimeError):
    """Raised after a truthful RANS smoke failure report is persisted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RANSSmokePipelineError(f"invalid {label}: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RANSSmokePipelineError(f"{label} root must be a JSON object")
    return payload


def _load_toml(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        content = path.read_bytes()
        payload = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RANSSmokePipelineError(f"invalid {label}: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RANSSmokePipelineError(f"{label} root must be a TOML table")
    return payload, hashlib.sha256(content).hexdigest()


def _finite(value: object, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise RANSSmokePipelineError(f"{label} must be finite")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise RANSSmokePipelineError(f"{label} must be finite") from error
    if not math.isfinite(numeric) or (positive and numeric <= 0.0):
        raise RANSSmokePipelineError(f"{label} must be finite")
    return numeric


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _marker_for_role(markers: Mapping[str, Mapping[str, Any]], role: object) -> str:
    role_text = str(role).strip() if isinstance(role, str) else ""
    matches = [
        name
        for name, specification in markers.items()
        if specification.get("dimension") == 2
        and specification.get("solver_boundary") is True
        and specification.get("role") == role_text
    ]
    if len(matches) != 1:
        raise RANSSmokePipelineError(
            f"role {role_text!r} must map to exactly one solver marker"
        )
    return matches[0]


def _require_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RANSSmokePipelineError(f"{label} must be a table/object")
    return value


def _command_record(result: CommandResult) -> dict[str, Any]:
    return {
        "executable": result.executable,
        "arguments": list(result.args),
        "command": list(result.command),
        "cwd": result.cwd,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "return_code": result.returncode,
        "timed_out": result.timed_out,
        "stdout_log": str(result.stdout_log),
        "stderr_log": str(result.stderr_log),
        "command_log": str(result.metadata_log),
    }


def _specific_heat_ratio(stdout: str) -> float:
    for line in stdout.splitlines():
        if "Spec. Heat Ratio" not in line:
            continue
        values = re.findall(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", line)
        if values:
            gamma = float(values[-1])
            if math.isfinite(gamma) and gamma > 1.0:
                return gamma
    raise RANSSmokePipelineError("SU2 stdout has no parseable specific heat ratio")


def _validate_rans_config(
    document: Mapping[str, Any],
    *,
    project: Mapping[str, Any],
    max_iterations: int,
) -> dict[str, Any]:
    if document.get("schema") != "cfdpipe.rans_smoke.v1":
        raise RANSSmokePipelineError("RANS smoke config schema is not v1")
    if document.get("status") != "APPROVED_DIAGNOSTIC_ONLY":
        raise RANSSmokePipelineError("RANS smoke config is not approved for diagnostics")
    scope = _require_mapping(document.get("scope"), label="RANS scope")
    numerics = _require_mapping(document.get("numerics"), label="RANS numerics")
    boundary = _require_mapping(
        document.get("boundary_contract"), label="RANS boundary contract"
    )
    wall = _require_mapping(document.get("wall_contract"), label="RANS wall contract")
    yplus = _require_mapping(document.get("yplus_gate"), label="RANS y+ gate")
    measurement_gate = _require_mapping(
        document.get("measurement_gate"), label="RANS measurement gate"
    )
    output = _require_mapping(document.get("output"), label="RANS output")
    physics = _require_mapping(project.get("physics"), label="project physics")
    project_boundaries = _require_mapping(
        project.get("boundaries"), label="project boundaries"
    )
    maximum_3d_elements = scope.get("maximum_3d_elements")
    if (
        scope.get("case_id") != "M50_H21_A8_B0"
        or scope.get("mesh_level") != "boundary_layer_smoke"
        or scope.get("solver") != "RANS"
        or scope.get("turbulence_model") != "SST"
        or scope.get("nproc") != 1
        or scope.get("gpu_enabled") is not False
        or scope.get("production_eligible") is not False
        or scope.get("physical_convergence_claim_allowed") is not False
        or isinstance(maximum_3d_elements, bool)
        or not isinstance(maximum_3d_elements, int)
        or not 0 < maximum_3d_elements <= 300_000
    ):
        raise RANSSmokePipelineError("RANS smoke scope is not bounded serial SST")
    startup_iterations = scope.get("startup_iterations")
    maximum_iterations = scope.get("maximum_diagnostic_iterations")
    if (
        isinstance(startup_iterations, bool)
        or not isinstance(startup_iterations, int)
        or startup_iterations != 20
        or isinstance(maximum_iterations, bool)
        or not isinstance(maximum_iterations, int)
        or not startup_iterations <= max_iterations <= maximum_iterations <= 500
    ):
        raise RANSSmokePipelineError("requested RANS iterations violate the 20..500 gate")
    timeout = _finite(scope.get("timeout_seconds"), label="scope timeout", positive=True)
    if timeout > 840.0:
        raise RANSSmokePipelineError("RANS smoke timeout exceeds 840 seconds")
    if numerics.get("spatial_order") != 1 or numerics.get("cfl_adapt") is not False:
        raise RANSSmokePipelineError("RANS smoke must remain first order with fixed CFL")
    if _finite(numerics.get("cfl_number"), label="CFL", positive=True) > 1.0:
        raise RANSSmokePipelineError("RANS smoke CFL must not exceed 1.0")
    if (
        boundary.get("farfield_role") != project_boundaries.get("farfield_role")
        or boundary.get("wall_role") != project_boundaries.get("wall_role")
        or boundary.get("rear_outlet_roles")
        != project_boundaries.get("rear_outlet_roles")
        or boundary.get("rear_outlet_mode")
        != "PROVISIONAL_SUPERSONIC_DIAGNOSTIC_ONLY"
        or boundary.get("su2_rear_outlet_option") != "MARKER_SUPERSONIC_OUTLET"
        or boundary.get("allow_static_outlet_pressure") is not False
        or boundary.get("allow_back_pressure") is not False
        or boundary.get("boundary_mode_frozen") is not False
    ):
        raise RANSSmokePipelineError("RANS smoke boundary contract differs from project")
    if (
        physics.get("solver") != "RANS"
        or physics.get("turbulence_model") != "SST"
        or wall.get("velocity") != physics.get("wall_velocity")
        or wall.get("thermal") != physics.get("wall_thermal")
        or wall.get("su2_option") != "MARKER_HEATFLUX"
        or float(wall.get("heat_flux_w_m2", math.nan)) != 0.0
    ):
        raise RANSSmokePipelineError("RANS smoke wall/physics contract is invalid")
    target = _finite(yplus.get("target"), label="target y+", positive=True)
    maximum = _finite(yplus.get("maximum"), label="maximum y+", positive=True)
    if (
        target != float(physics.get("target_yplus", math.nan))
        or maximum != float(physics.get("maximum_yplus", math.nan))
        or target > maximum
        or yplus.get("require_all_wall_samples_finite") is not True
        or yplus.get("require_positive_sample") is not True
        or float(yplus.get("maximum_exceedance_area_fraction", math.nan)) != 0.0
    ):
        raise RANSSmokePipelineError("RANS smoke y+ contract differs from project")
    fields = output.get("volume_fields")
    required_arrays = output.get("required_point_arrays")
    files = output.get("output_files")
    if (
        not isinstance(fields, list)
        or fields != ["COORDINATES", "SOLUTION", "PRIMITIVE"]
        or not isinstance(required_arrays, list)
        or set(required_arrays)
        != {
            "Density",
            "Momentum",
            "Velocity",
            "Mach",
            "Pressure",
            "Temperature",
            "Y_Plus",
            "Skin_Friction_Coefficient",
        }
        or len(required_arrays) != 8
        or not isinstance(files, list)
        or set(files) != {"RESTART", "PARAVIEW", "SURFACE_PARAVIEW"}
        or output.get("solution_prefix") != "rans_solution"
        or output.get("history_basename") != "history"
        or output.get("write_frequency") != 1
    ):
        raise RANSSmokePipelineError("RANS smoke output contract is incomplete")
    _finite(
        measurement_gate.get("maximum_relative_area_error"),
        label="measurement area tolerance",
        positive=True,
    )
    return {
        "scope": dict(scope),
        "numerics": dict(numerics),
        "boundary": dict(boundary),
        "wall": dict(wall),
        "yplus": dict(yplus),
        "measurement_gate": dict(measurement_gate),
        "output": dict(output),
        "provenance": dict(
            _require_mapping(document.get("provenance"), label="RANS provenance")
        ),
    }


def _validate_mesh_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    provenance: Mapping[str, Any],
    input_records: Mapping[str, Mapping[str, Any]],
    expected_markers: set[str],
    maximum_elements: int,
) -> tuple[Path, dict[str, Any]]:
    if (
        manifest.get("status") != "PASS"
        or manifest.get("diagnostic_quality_status") != "PASS"
        or manifest.get("smoke_only") is not True
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("requires_quality_improvement_before_production") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
    ):
        raise RANSSmokePipelineError("boundary-layer mesh manifest is not a clean PASS")
    outputs = _require_mapping(manifest.get("outputs"), label="mesh outputs")
    mesh_record = _require_mapping(outputs.get("mesh.su2"), label="mesh.su2 output")
    raw_mesh = mesh_record.get("path")
    if not isinstance(raw_mesh, str):
        raise RANSSmokePipelineError("mesh manifest has no mesh.su2 path")
    mesh = Path(raw_mesh).expanduser().resolve(strict=True)
    mesh_sha = _sha256(mesh)
    if mesh_record.get("sha256") != mesh_sha:
        raise RANSSmokePipelineError("boundary-layer mesh SHA256 is stale")
    historical_pair_matches = (
        provenance.get("boundary_layer_mesh_manifest_sha256")
        == _sha256(manifest_path)
        and provenance.get("boundary_layer_mesh_sha256") == mesh_sha
    )
    if not historical_pair_matches:
        _validate_regenerated_mesh_lineage(
            manifest, provenance=provenance, input_records=input_records
        )
    validation = dict(
        _require_mapping(manifest.get("su2_validation"), label="SU2 mesh validation")
    )
    if (
        validation.get("status") != "PASS"
        or validation.get("ndime") != 3
        or not isinstance(validation.get("nelem"), int)
        or not 0 < int(validation["nelem"]) <= maximum_elements
        or not isinstance(validation.get("npoin"), int)
        or int(validation["npoin"]) <= 0
        or set(_require_mapping(validation.get("marker_element_counts"), label="markers"))
        != expected_markers
    ):
        raise RANSSmokePipelineError("boundary-layer SU2 validation contract failed")
    for label in ("generation_audit", "readback_audit"):
        audit = _require_mapping(manifest.get(label), label=label)
        quality = _require_mapping(audit.get("quality"), label=f"{label} quality")
        if (
            audit.get("status") != "PASS"
            or audit.get("requested_layer_count") != 15
            or audit.get("element_count_3d") != validation["nelem"]
            or quality.get("nonpositive_volume_count") != 0
            or quality.get("nonpositive_jacobian_count") != 0
            or quality.get("nonfinite_count") != 0
        ):
            raise RANSSmokePipelineError(f"{label} does not satisfy the RANS preflight")
    parsed = parse_topology_smoke_su2(mesh)
    if (
        len(parsed.points) != validation["npoin"]
        or len(parsed.tetrahedra) + len(parsed.prisms) != validation["nelem"]
        or set(parsed.markers) != expected_markers
    ):
        raise RANSSmokePipelineError("independent mixed-mesh parser differs from manifest")
    return mesh, validation


def _validate_regenerated_mesh_lineage(
    manifest: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any],
    input_records: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind a fresh smoke mesh to the same frozen configs and private geometry."""

    run_evidence = _require_mapping(
        manifest.get("run_evidence"), label="regenerated mesh run evidence"
    )
    configuration_files = _require_mapping(
        run_evidence.get("configuration_files"),
        label="regenerated mesh configuration files",
    )
    expected_configuration_hashes = {
        "smoke": provenance.get("boundary_layer_smoke_sha256"),
        "schedule": provenance.get("boundary_layer_trial_sha256"),
        "topology_smoke": provenance.get("topology_smoke_sha256"),
    }
    if run_evidence.get("downstream_programs_called") is not False or any(
        _require_mapping(configuration_files.get(name), label=f"{name} config").get(
            "sha256"
        )
        != expected_hash
        for name, expected_hash in expected_configuration_hashes.items()
    ):
        raise RANSSmokePipelineError(
            "regenerated boundary-layer mesh config lineage is stale"
        )

    recorded_inputs = _require_mapping(
        run_evidence.get("input_records"), label="regenerated mesh input records"
    )
    expected_input_hashes = {
        "project": input_records["project"]["sha256"],
        "cases": input_records["cases"]["sha256"],
        "markers": input_records["markers"]["sha256"],
        "topology_smoke_config": input_records["topology_smoke_config"]["sha256"],
        "step": input_records["step"]["sha256"],
        "pipeline_geometry": input_records["pipeline_geometry"]["sha256"],
    }
    if any(
        _require_mapping(recorded_inputs.get(name), label=f"{name} input").get(
            "sha256"
        )
        != expected_hash
        for name, expected_hash in expected_input_hashes.items()
    ):
        raise RANSSmokePipelineError(
            "regenerated boundary-layer mesh input lineage is stale"
        )

    normalized = _require_mapping(
        manifest.get("normalized_config"), label="regenerated normalized config"
    )
    normalized_provenance = _require_mapping(
        normalized.get("provenance"), label="regenerated normalized provenance"
    )
    expected_normalized_provenance = {
        "project_sha256": expected_input_hashes["project"],
        "cases_sha256": expected_input_hashes["cases"],
        "markers_sha256": expected_input_hashes["markers"],
        "topology_smoke_sha256": expected_input_hashes["topology_smoke_config"],
        "pipeline_brep_sha256": expected_input_hashes["pipeline_geometry"],
    }
    source_before = _require_mapping(
        manifest.get("source_before"), label="regenerated source before"
    )
    source_after = _require_mapping(
        manifest.get("source_after"), label="regenerated source after"
    )
    if (
        dict(normalized_provenance) != expected_normalized_provenance
        or manifest.get("source_unchanged") is not True
        or source_before.get("sha256") != expected_input_hashes["pipeline_geometry"]
        or source_after != source_before
        or source_before.get("read_only") is not True
    ):
        raise RANSSmokePipelineError(
            "regenerated boundary-layer mesh private geometry lineage is stale"
        )


def _validate_provenance(
    provenance: Mapping[str, Any],
    *,
    input_records: Mapping[str, Mapping[str, Any]],
    config_paths: Mapping[str, Path],
    outlet_evidence_path: Path,
) -> dict[str, Any]:
    expected = {
        "project_sha256": input_records["project"]["sha256"],
        "cases_sha256": input_records["cases"]["sha256"],
        "markers_sha256": input_records["markers"]["sha256"],
        "topology_smoke_sha256": input_records["topology_smoke_config"]["sha256"],
        "boundary_layer_trial_sha256": _sha256(config_paths["boundary_layer_trial"]),
        "boundary_layer_smoke_sha256": _sha256(config_paths["boundary_layer_smoke"]),
        "rear_outlet_pilot_sha256": _sha256(config_paths["rear_outlet_pilot"]),
        "outlet_validation_manifest_sha256": _sha256(outlet_evidence_path),
    }
    stale = sorted(key for key, value in expected.items() if provenance.get(key) != value)
    if stale:
        raise RANSSmokePipelineError(
            "RANS smoke provenance is stale for: " + ", ".join(stale)
        )
    return {
        key: {"path": str(config_paths.get(key.removesuffix("_sha256"), "")), "sha256": value}
        for key, value in expected.items()
    }


def _apply_result(manifest: dict[str, Any], result: CommandResult) -> None:
    manifest.update(_command_record(result))


def _validated_restart_source(
    manifest_path: Path,
    *,
    repository_root: Path,
    expected_mesh_sha256: str,
    expected_case_id: str,
) -> dict[str, Any]:
    """Resolve one explicit PASS RANS restart without searching other runs."""

    runs_root = (repository_root / "runs" / "rans_smoke").resolve(strict=True)
    manifest = manifest_path.expanduser().resolve(strict=True)
    if not _within(manifest, runs_root) or manifest.name != "run_manifest.json":
        raise RANSSmokePipelineError(
            "restart manifest must be an explicit run_manifest.json under runs/rans_smoke"
        )
    document = _load_json(manifest, label="restart source SU2 manifest")
    if (
        document.get("status") != "PASS"
        or document.get("solver_model") != "RANS"
        or document.get("turbulence_model") != "SST"
        or document.get("mesh_sha256") != expected_mesh_sha256
        or document.get("return_code") != 0
        or document.get("timed_out") is not False
        or document.get("fatal_matches") != []
    ):
        raise RANSSmokePipelineError(
            "restart source SU2 manifest is not a compatible PASS RANS/SST run"
        )
    preparation = _require_mapping(
        document.get("preparation"), label="restart source preparation"
    )
    selected_case = _require_mapping(
        preparation.get("selected_case"), label="restart source selected case"
    )
    if selected_case.get("case_id") != expected_case_id:
        raise RANSSmokePipelineError("restart source case differs from requested case")
    if (
        preparation.get("nproc") != 1
        or preparation.get("gpu_enabled") is not False
        or preparation.get("boundary_mode_frozen") is not False
        or preparation.get("outlet_pressure_or_backpressure_set") is not False
    ):
        raise RANSSmokePipelineError("restart source scope is not the bounded serial gate")

    restart_value = document.get("restart_file")
    restart_sha = document.get("restart_sha256")
    if not isinstance(restart_value, str) or not isinstance(restart_sha, str):
        raise RANSSmokePipelineError("restart source manifest lacks restart evidence")
    restart = Path(restart_value).expanduser().resolve(strict=True)
    if restart.parent != manifest.parent or not restart.is_file() or restart.stat().st_size <= 0:
        raise RANSSmokePipelineError("restart source must be a non-empty sibling of its manifest")
    if _sha256(restart) != restart_sha:
        raise RANSSmokePipelineError("restart source SHA256 differs from its manifest")

    source_config_value = document.get("config_path")
    if not isinstance(source_config_value, str):
        raise RANSSmokePipelineError("restart source manifest lacks config_path")
    source_config = Path(source_config_value).expanduser().resolve(strict=True)
    if source_config.parent != manifest.parent:
        raise RANSSmokePipelineError("restart source config must be a sibling of its manifest")
    source_config_text = source_config.read_text(encoding="utf-8")
    required_lines = (
        "SOLVER= RANS",
        "KIND_TURB_MODEL= SST",
        "MUSCL_FLOW= NO",
        "MUSCL_TURB= NO",
        "MARKER_SUPERSONIC_OUTLET=",
    )
    if any(token not in source_config_text for token in required_lines):
        raise RANSSmokePipelineError(
            "restart source config is not the verified first-order SST contract"
        )
    if re.search(
        r"(?im)^\s*(?:MARKER_OUTLET|OUTLET_PRESSURE|BACK_PRESSURE|ENABLE_CUDA)\s*=",
        source_config_text,
    ):
        raise RANSSmokePipelineError("restart source config contains a forbidden assignment")

    report = manifest.parent.parent / "rans_smoke_report.json"
    report = report.resolve(strict=True)
    report_document = _load_json(report, label="restart source RANS report")
    report_su2 = _require_mapping(report_document.get("su2"), label="restart report SU2")
    if (
        report_document.get("status") != "PASS"
        or report_document.get("startup_gate") != "PASS"
        or report_document.get("yplus_gate") != "PASS"
        or report_document.get("diagnostic_only") is not True
        or report_document.get("production_eligible") is not False
        or report_su2.get("restart_sha256") != restart_sha
        or report_su2.get("mesh_sha256") != expected_mesh_sha256
    ):
        raise RANSSmokePipelineError(
            "restart source parent report is not a compatible PASS y+ diagnostic"
        )
    return {
        "manifest_path": str(manifest),
        "manifest_sha256": _sha256(manifest),
        "report_path": str(report),
        "report_sha256": _sha256(report),
        "config_path": str(source_config),
        "config_sha256": _sha256(source_config),
        "restart_path": str(restart),
        "restart_sha256": restart_sha,
        "source_iterations": int(document.get("iterations", 0)),
        "source_spatial_order": 1,
    }


def _validate_binary_restart(path: Path, *, expected_points: int) -> dict[str, Any]:
    """Validate the finite SU2 binary restart layout used by this installation."""

    restart = path.expanduser().resolve(strict=True)
    size = restart.stat().st_size
    with restart.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20:
            raise RANSSmokePipelineError("SU2 restart header is truncated")
        magic, field_count, point_count, metadata_1, metadata_2 = struct.unpack(
            "<5i", header
        )
        if magic != 535532:
            raise RANSSmokePipelineError("SU2 restart magic differs from 535532")
        if not 1 <= field_count <= 128 or point_count != expected_points:
            raise RANSSmokePipelineError(
                "SU2 restart field/point counts differ from the mesh contract"
            )
        raw_names = stream.read(33 * field_count)
        if len(raw_names) != 33 * field_count:
            raise RANSSmokePipelineError("SU2 restart field-name table is truncated")
        fields = tuple(
            raw_names[index * 33 : (index + 1) * 33]
            .split(b"\0", 1)[0]
            .decode("ascii", errors="strict")
            for index in range(field_count)
        )
        required = {
            "x",
            "y",
            "z",
            "Density",
            "Momentum_x",
            "Momentum_y",
            "Momentum_z",
            "Energy",
            "Turb_Kin_Energy",
            "Omega",
        }
        if set(fields) != required or field_count != len(required):
            raise RANSSmokePipelineError(
                "SU2 restart fields are not the expected 3D RANS/SST state"
            )
        expected_size = 20 + 33 * field_count + 8 * field_count * point_count
        if size != expected_size:
            raise RANSSmokePipelineError(
                f"SU2 restart length {size} differs from expected {expected_size}"
            )
        value_count = field_count * point_count
        checked = 0
        while checked < value_count:
            count = min(65_536, value_count - checked)
            payload = stream.read(8 * count)
            if len(payload) != 8 * count:
                raise RANSSmokePipelineError("SU2 restart values are truncated")
            if any(not math.isfinite(value) for (value,) in struct.iter_unpack("<d", payload)):
                raise RANSSmokePipelineError("SU2 restart contains NaN or Inf")
            checked += count
        if stream.read(1):
            raise RANSSmokePipelineError("SU2 restart has an unexpected trailing payload")
    return {
        "path": str(restart),
        "sha256": _sha256(restart),
        "size_bytes": size,
        "format": "SU2_BINARY_RESTART_LITTLE_ENDIAN",
        "magic": magic,
        "field_count": field_count,
        "point_count": point_count,
        "metadata": [metadata_1, metadata_2],
        "fields": list(fields),
        "finite_value_count": value_count,
        "finite": True,
        "iteration_index_embedded": False,
    }


def _restart_semantics_evidence(su2_cfd: Path) -> list[dict[str, Any]]:
    """Bind restart stem/extension behavior to shipped SU2 8.5 Python sources."""

    executable = su2_cfd.expanduser().resolve(strict=True)
    candidates = (
        (executable.parent / "SU2" / "run" / "direct.py", ("SOLUTION_FILENAME", "RESTART_SOL")),
        (
            executable.parent / "SU2" / "io" / "state.py",
            ("READ_BINARY_RESTART", "SOLUTION_FILENAME", '".dat"'),
        ),
        (executable.parent / "compute_polar.py", ("RESTART_SOL", ".dat")),
    )
    evidence: list[dict[str, Any]] = []
    for path, tokens in candidates:
        resolved = path.resolve(strict=True)
        content = resolved.read_text(encoding="utf-8", errors="strict")
        if any(token not in content for token in tokens):
            raise RANSSmokePipelineError(
                f"installed SU2 restart semantics evidence is incomplete: {resolved}"
            )
        evidence.append(
            {
                "path": str(resolved),
                "sha256": _sha256(resolved),
                "required_tokens": list(tokens),
            }
        )
    return evidence


def run_rans_smoke_validation(
    *,
    step_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str],
    cases_path: str | os.PathLike[str],
    markers_path: str | os.PathLike[str],
    topology_smoke_path: str | os.PathLike[str],
    boundary_layer_trial_path: str | os.PathLike[str],
    boundary_layer_smoke_path: str | os.PathLike[str],
    rear_outlet_pilot_path: str | os.PathLike[str],
    rans_smoke_path: str | os.PathLike[str],
    mesh_manifest_path: str | os.PathLike[str],
    outlet_evidence_path: str | os.PathLike[str],
    case_id: str,
    max_iterations: int,
    output_directory: str | os.PathLike[str],
    trusted_repository_root: str | os.PathLike[str],
    toolchain: Toolchain,
    paraview_script_directory: str | os.PathLike[str],
    timeout_seconds: float | None = None,
    live_output: bool = True,
    controller_argv: Sequence[str] = (),
    spatial_order: int = 1,
    restart_manifest_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run one new, isolated serial RANS smoke directory and fail closed."""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if isinstance(spatial_order, bool) or spatial_order not in (1, 2):
        raise ValueError("RANS smoke spatial_order must be 1 or 2")
    if spatial_order == 1 and restart_manifest_path is not None:
        raise ValueError("first-order RANS smoke must not consume a restart manifest")
    if spatial_order == 2 and restart_manifest_path is None:
        raise ValueError("second-order RANS smoke requires a restart manifest")
    repository_root = Path(trusted_repository_root).expanduser().resolve(strict=True)
    allowed_root = (repository_root / "runs" / "rans_smoke").resolve(strict=False)
    raw_output = Path(output_directory).expanduser()
    if ".." in raw_output.parts:
        raise ValueError("RANS smoke output must not contain '..'")
    output = Path(os.path.abspath(raw_output)).resolve(strict=False)
    if output == allowed_root or not _within(output, allowed_root):
        raise ValueError(f"RANS smoke output must be a new child of {allowed_root}")
    if output.exists():
        raise ValueError(f"RANS smoke refuses to overwrite existing output: {output}")
    output.mkdir(parents=True)
    report_path = output / "rans_smoke_report.json"
    report: dict[str, Any] = {
        "schema": "cfdpipe.rans_smoke_report.v1",
        "status": "FAIL",
        "startup_gate": "FAIL",
        "yplus_gate": "NOT_RUN",
        "diagnostic_only": True,
        "production_eligible": False,
        "convergence_claimed": False,
        "spatial_order": spatial_order,
        "restart_used": spatial_order == 2,
        "restart_lineage": None,
        "started_at": _utc_now(),
        "ended_at": None,
        "controller_argv": list(controller_argv),
        "inputs": {},
        "tools": {},
        "su2": None,
        "paraview": {"status": "NOT_RUN"},
        "error": None,
    }
    tracked_sources: dict[str, Path] = {}
    source_before: dict[str, str] = {}
    try:
        inputs = load_real_connection_inputs(
            step_path=step_path,
            project_path=project_path,
            cases_path=cases_path,
            markers_path=markers_path,
            topology_smoke_path=topology_smoke_path,
            case_id=case_id,
            trusted_repository_root=repository_root,
        )
        selected_case = select_real_case(inputs)
        production = _require_mapping(inputs.project.get("production"), label="production")
        if production.get("design_case") != case_id:
            raise RANSSmokePipelineError("RANS smoke is limited to the design case")
        config_paths = {
            "boundary_layer_trial": Path(boundary_layer_trial_path).resolve(strict=True),
            "boundary_layer_smoke": Path(boundary_layer_smoke_path).resolve(strict=True),
            "rear_outlet_pilot": Path(rear_outlet_pilot_path).resolve(strict=True),
            "rans_smoke": Path(rans_smoke_path).resolve(strict=True),
        }
        config_root = (repository_root / "config").resolve(strict=True)
        if any(path.parent != config_root for path in config_paths.values()):
            raise RANSSmokePipelineError("all RANS gate configs must be under config/")
        rans_document, rans_config_sha = _load_toml(
            config_paths["rans_smoke"], label="RANS smoke config"
        )
        normalized = _validate_rans_config(
            rans_document, project=inputs.project, max_iterations=max_iterations
        )
        provenance = normalized["provenance"]

        outlet_path = Path(outlet_evidence_path).resolve(strict=True)
        outlet_evidence = _load_json(outlet_path, label="outlet validation evidence")
        if (
            outlet_evidence.get("status") != "PASS"
            or outlet_evidence.get("diagnostic_gate") != "PASS"
            or outlet_evidence.get("production_boundary_frozen") is not False
            or outlet_evidence.get("production_eligible") is not False
            or not all(
                bool(value)
                for value in _require_mapping(
                    outlet_evidence.get("criteria"), label="outlet criteria"
                ).values()
            )
        ):
            raise RANSSmokePipelineError("prior bounded outlet evidence is not PASS")
        pilot_document, _ = _load_toml(
            config_paths["rear_outlet_pilot"], label="rear outlet pilot"
        )
        pilot_scope = _require_mapping(pilot_document.get("scope"), label="pilot scope")
        pilot_boundary = _require_mapping(
            pilot_document.get("rear_outlet_boundary"), label="pilot boundary"
        )
        if (
            pilot_document.get("status") != "APPROVED_FOR_CONNECTION_ONLY"
            or pilot_scope.get("case_id") != case_id
            or pilot_boundary.get("su2_option") != "MARKER_SUPERSONIC_OUTLET"
            or pilot_boundary.get("allow_static_pressure") is not False
            or pilot_boundary.get("allow_back_pressure") is not False
            or pilot_boundary.get("boundary_mode_frozen") is not False
        ):
            raise RANSSmokePipelineError("rear outlet pilot contract is not safe")
        _validate_provenance(
            provenance,
            input_records=inputs.input_records,
            config_paths=config_paths,
            outlet_evidence_path=outlet_path,
        )

        project_boundaries = _require_mapping(
            inputs.project.get("boundaries"), label="project boundaries"
        )
        farfield = _marker_for_role(inputs.markers, project_boundaries.get("farfield_role"))
        wall = _marker_for_role(inputs.markers, project_boundaries.get("wall_role"))
        rear_roles = project_boundaries.get("rear_outlet_roles")
        if not isinstance(rear_roles, list) or len(rear_roles) != 2:
            raise RANSSmokePipelineError("project must define exactly two rear outlets")
        rear_outlets = tuple(_marker_for_role(inputs.markers, role) for role in rear_roles)
        expected_markers = {farfield, wall, *rear_outlets}

        mesh_manifest_file = Path(mesh_manifest_path).resolve(strict=True)
        runs_root = (repository_root / "runs").resolve(strict=True)
        if not _within(mesh_manifest_file, runs_root):
            raise RANSSmokePipelineError("mesh manifest must stay under repository runs")
        mesh_manifest = _load_json(mesh_manifest_file, label="boundary-layer mesh manifest")
        max_elements = int(normalized["scope"]["maximum_3d_elements"])
        source_mesh, mesh_validation = _validate_mesh_manifest(
            mesh_manifest,
            manifest_path=mesh_manifest_file,
            provenance=provenance,
            input_records=inputs.input_records,
            expected_markers=expected_markers,
            maximum_elements=max_elements,
        )
        restart_source: dict[str, Any] | None = None
        if spatial_order == 2:
            restart_source = _validated_restart_source(
                Path(str(restart_manifest_path)),
                repository_root=repository_root,
                expected_mesh_sha256=_sha256(source_mesh),
                expected_case_id=case_id,
            )
            restart_source["binary_validation"] = _validate_binary_restart(
                Path(restart_source["restart_path"]),
                expected_points=int(mesh_validation["npoin"]),
            )
            report["restart_lineage"] = {
                "used": True,
                "source_manifest_sha256": restart_source["report_sha256"],
                "source_su2_manifest_sha256": restart_source["manifest_sha256"],
                "input_restart_sha256": restart_source["restart_sha256"],
            }

        atmosphere_config = _require_mapping(
            inputs.project.get("atmosphere"), label="project atmosphere"
        )
        altitude_km = _finite(selected_case.get("altitude_km"), label="altitude")
        atmosphere = us_standard_atmosphere_1976(
            1000.0 * altitude_km,
            model=str(atmosphere_config.get("model", "")),
            altitude_kind=str(atmosphere_config.get("altitude_kind", "")),
            viscosity_model=str(atmosphere_config.get("viscosity_model", "")),
        )
        reference = _require_mapping(inputs.project.get("reference"), label="reference")
        area = _finite(reference.get("area_m2"), label="reference area", positive=True)
        length = _finite(reference.get("length_m"), label="reference length", positive=True)
        origin_raw = reference.get("moment_origin_m")
        if not isinstance(origin_raw, list) or len(origin_raw) != 3:
            raise RANSSmokePipelineError("reference moment origin must have three values")
        origin = tuple(_finite(value, label="moment origin") for value in origin_raw)
        measurement_role = project_boundaries.get("measurement_surface_role")
        measurement_matches = [
            value
            for value in inputs.measurements.values()
            if value.get("role") == measurement_role
        ]
        if len(measurement_matches) != 1:
            raise RANSSmokePipelineError("project measurement role is missing or ambiguous")
        measurement = measurement_matches[0]
        geometry = _require_mapping(measurement.get("geometry"), label="measurement geometry")
        if (
            measurement.get("solver_boundary") is not False
            or measurement.get("postprocess_only") is not True
            or geometry.get("plane_axis") != "x"
            or geometry.get("normal_unit") != [1.0, 0.0, 0.0]
        ):
            raise RANSSmokePipelineError("measurement surface contract is invalid")

        tracked_sources = {
            "step": inputs.step_path,
            "pipeline_geometry": inputs.pipeline_geometry_path,
            "project": inputs.project_path,
            "cases": inputs.cases_path,
            "markers": inputs.markers_path,
            "topology_smoke": inputs.topology_smoke_path,
            **config_paths,
            "mesh_manifest": mesh_manifest_file,
            "source_mesh": source_mesh,
            "outlet_evidence": outlet_path,
            "paraview_script": (
                Path(paraview_script_directory).resolve(strict=True)
                / "rans_smoke_diagnostics.py"
            ).resolve(strict=True),
        }
        if restart_source is not None:
            tracked_sources.update(
                {
                    "restart_source_manifest": Path(
                        restart_source["manifest_path"]
                    ),
                    "restart_source_report": Path(restart_source["report_path"]),
                    "restart_source_config": Path(restart_source["config_path"]),
                    "restart_source_file": Path(restart_source["restart_path"]),
                }
            )
        su2_tool = toolchain.resolve("su2_cfd")
        pvbatch_tool = toolchain.resolve("pvbatch")
        restart_semantics: list[dict[str, Any]] = []
        if restart_source is not None:
            restart_semantics = _restart_semantics_evidence(Path(su2_tool.path))
            tracked_sources.update(
                {
                    f"restart_semantics_{index}": Path(record["path"])
                    for index, record in enumerate(restart_semantics, start=1)
                }
            )
        source_before = {name: _sha256(path) for name, path in tracked_sources.items()}
        report["tools"] = {
            "su2_cfd": su2_tool.as_dict(),
            "pvbatch": pvbatch_tool.as_dict(),
            "restart_semantics_evidence": restart_semantics,
        }
        report["inputs"] = {
            "source_records": inputs.input_records,
            "selected_case": dict(selected_case),
            "rans_config": {
                "path": str(config_paths["rans_smoke"]),
                "sha256": rans_config_sha,
                "configured_spatial_order": int(
                    normalized["numerics"]["spatial_order"]
                ),
                "normalized": {
                    **normalized,
                    "numerics": {
                        **normalized["numerics"],
                        "spatial_order": spatial_order,
                    },
                },
            },
            "mesh_manifest": {
                "path": str(mesh_manifest_file),
                "sha256": _sha256(mesh_manifest_file),
            },
            "mesh": {"path": str(source_mesh), "sha256": _sha256(source_mesh)},
            "outlet_evidence": {"path": str(outlet_path), "sha256": _sha256(outlet_path)},
            "restart_source": restart_source,
            "restart_semantics_evidence": restart_semantics,
        }

        timeout = (
            float(normalized["scope"]["timeout_seconds"])
            if timeout_seconds is None
            else _finite(timeout_seconds, label="timeout", positive=True)
        )
        if timeout > float(normalized["scope"]["timeout_seconds"]):
            raise RANSSmokePipelineError("CLI timeout exceeds the RANS config limit")
        su2_directory = output / "su2"
        su2_directory.mkdir()
        local_mesh = su2_directory / "mesh.su2"
        config_path = su2_directory / "case.cfg"
        shutil.copy2(source_mesh, local_mesh)
        if _sha256(local_mesh) != _sha256(source_mesh):
            raise RANSSmokePipelineError("local mesh copy SHA256 mismatch")
        local_restart: Path | None = None
        if restart_source is not None:
            local_restart = su2_directory / "rans_input_restart.dat"
            shutil.copy2(Path(restart_source["restart_path"]), local_restart)
            if _sha256(local_restart) != restart_source["restart_sha256"]:
                raise RANSSmokePipelineError("local restart copy SHA256 mismatch")
        syntax_reference = discover_su2_rans_reference(su2_tool.path)
        numerics = normalized["numerics"]
        rendered, angle_mapping = render_project_rans_smoke_config(
            syntax_reference,
            selected_case,
            atmosphere,
            reference_area_m2=area,
            reference_length_m=length,
            moment_origin_m=origin,
            farfield_marker=farfield,
            wall_marker=wall,
            rear_outlet_markers=rear_outlets,
            max_iterations=max_iterations,
            cfl_number=float(numerics["cfl_number"]),
            gradient_method=str(numerics["gradient_method"]),
            flow_convective_method=str(numerics["flow_convective_method"]),
            turbulence_convective_method=str(numerics["turbulence_convective_method"]),
            flow_time_discretization=str(numerics["flow_time_discretization"]),
            turbulence_time_discretization=str(numerics["turbulence_time_discretization"]),
            linear_solver=str(numerics["linear_solver"]),
            linear_solver_preconditioner=str(numerics["linear_solver_preconditioner"]),
            linear_solver_error=float(numerics["linear_solver_error"]),
            linear_solver_iterations=int(numerics["linear_solver_iterations"]),
            freestream_turbulence_intensity=float(
                numerics["freestream_turbulence_intensity"]
            ),
            freestream_turbulent_to_laminar_viscosity_ratio=float(
                numerics["freestream_turbulent_to_laminar_viscosity_ratio"]
            ),
            volume_fields=normalized["output"]["volume_fields"],
            output_files=normalized["output"]["output_files"],
        )
        if restart_source is not None:
            rendered = render_project_rans_restart_config(
                rendered,
                syntax_reference,
                solution_filename="rans_input_restart",
                iterations=max_iterations,
                volume_prefix="rans_solution",
                surface_prefix="rans_surface",
                restart_prefix="rans_solution_restart",
            )
        config_path.write_text(rendered, encoding="utf-8")
        preparation = {
            "status": "PASS",
            "diagnostic_only": True,
            "production_eligible": False,
            "config_path": str(config_path),
            "config_sha256": _sha256(config_path),
            "mesh_path": str(local_mesh),
            "mesh_sha256": _sha256(local_mesh),
            "source_mesh_path": str(source_mesh),
            "source_mesh_sha256": _sha256(source_mesh),
            "requested_iterations": max_iterations,
            "spatial_order": spatial_order,
            "restart_used": restart_source is not None,
            "restart_input": None
            if restart_source is None
            else {
                **restart_source,
                "local_path": str(local_restart),
                "local_sha256": _sha256(local_restart),
                "local_binary_validation": _validate_binary_restart(
                    local_restart, expected_points=int(mesh_validation["npoin"])
                ),
            },
            "nproc": 1,
            "gpu_enabled": False,
            "selected_case": dict(selected_case),
            "atmosphere": atmosphere.as_dict(),
            "angle_mapping": angle_mapping.as_manifest(),
            "markers": {
                "farfield": farfield,
                "wall": wall,
                "rear_outlets": list(rear_outlets),
            },
            "reference_config": syntax_reference.as_manifest(),
            "boundary_mode_frozen": False,
            "outlet_pressure_or_backpressure_set": False,
        }
        _write_json(su2_directory / "preparation_manifest.json", preparation)

        runner = CommandRunner(
            su2_directory,
            live_output=live_output,
            allow_mnt_c_executables=toolchain.allow_mnt_c_executables,
        )
        bridge = SU2Bridge(runner, su2_tool.path)
        run_manifest_path = su2_directory / "run_manifest.json"
        su2_manifest: dict[str, Any] = {
            "status": "FAIL",
            "solver_model": "RANS",
            "turbulence_model": "SST",
            "diagnostic_only": True,
            "production_eligible": False,
            "convergence_claimed": False,
            "spatial_order": spatial_order,
            "restart_used": restart_source is not None,
            "restart_input": preparation["restart_input"],
            "restart_read_evidence": None,
            "su2_cfd_path": str(Path(su2_tool.path).resolve(strict=True)),
            "su2_version": None,
            "su2_version_evidence": None,
            "reference_config": syntax_reference.as_manifest(),
            "preparation": preparation,
            "config_path": str(config_path),
            "config_sha256": _sha256(config_path),
            "mesh_path": str(local_mesh),
            "mesh_sha256": _sha256(local_mesh),
            "mesh_validation": mesh_validation,
            "requested_iterations": max_iterations,
            "iterations": 0,
            "history_path": None,
            "history_sha256": None,
            "visualization_file": None,
            "visualization_validation": None,
            "surface_visualization_file": None,
            "surface_visualization_validation": None,
            "restart_file": None,
            "freestream_log_evidence": None,
            "gas_evidence": None,
            "fatal_matches": [],
            "nonfatal_diagnostics": [],
            "start_time": _utc_now(),
            "end_time": None,
            "error": None,
        }
        result: CommandResult | None = None
        try:
            version, version_result = bridge.probe_version(
                su2_directory, timeout_seconds=timeout
            )
            su2_manifest["su2_version"] = version
            su2_manifest["su2_version_evidence"] = version_result.as_metadata()
            visualization_before = snapshot_visualizations(su2_directory)
            result = bridge.run_su2(
                config_path,
                su2_directory,
                nproc=1,
                timeout_seconds=timeout,
                live_output=live_output,
            )
            _apply_result(su2_manifest, result)
            if result.returncode != 0:
                raise SU2BridgeError(
                    f"SU2_CFD returned {result.returncode}; stderr:\n{result.stderr}"
                )
            fatal = scan_fatal_output(result.stdout, result.stderr)
            su2_manifest["fatal_matches"] = fatal
            su2_manifest["nonfatal_diagnostics"] = scan_nonfatal_diagnostics(
                result.stdout, result.stderr
            )
            if fatal:
                raise SU2BridgeError("SU2 RANS log contains fatal text: " + " | ".join(fatal))
            stdout_folded = result.stdout.casefold()
            if restart_source is not None:
                restart_failures = (
                    "no restart solution",
                    "cannot open restart",
                    "unable to open restart",
                    "restart file not found",
                )
                if any(token in stdout_folded for token in restart_failures):
                    raise SU2BridgeError("SU2 reported that the restart input was not read")
                restart_lines = [
                    line
                    for line in result.stdout.splitlines()
                    if "restart" in line.casefold()
                    and "rans_input_restart" in line.casefold()
                ]
                if not restart_lines:
                    raise SU2BridgeError(
                        "SU2 stdout lacks positive rans_input_restart read evidence"
                    )
                if local_restart is None or _sha256(local_restart) != restart_source[
                    "restart_sha256"
                ]:
                    raise SU2BridgeError("SU2 changed the read-only local restart input")
                su2_manifest["restart_read_evidence"] = {
                    "lines": restart_lines,
                    "input_sha256_after_solver": _sha256(local_restart),
                    "input_unchanged": True,
                }
            if "sst" not in stdout_folded or "sutherland" not in stdout_folded:
                raise SU2BridgeError("SU2 stdout lacks actual SST/Sutherland model evidence")
            freestream = parse_su2_freestream_log(result.stdout)
            expected_unit = tuple(angle_mapping.project_unit_vector_xyz)
            actual_unit = tuple(float(value) for value in freestream["unit_vector_xyz"])
            component_error = max(
                abs(expected - actual)
                for expected, actual in zip(expected_unit, actual_unit, strict=True)
            )
            freestream["expected_unit_vector_xyz"] = list(expected_unit)
            freestream["maximum_unit_vector_component_error"] = component_error
            su2_manifest["freestream_log_evidence"] = freestream
            if component_error > 1.0e-4:
                raise SU2BridgeError("SU2 freestream direction differs from project mapping")
            gamma = _specific_heat_ratio(result.stdout)
            su2_manifest["gas_evidence"] = {
                "specific_heat_ratio": gamma,
                "source": "solver.stdout.log Spec. Heat Ratio row",
                "stdout_sha256": _sha256(result.stdout_log),
            }
            history = su2_directory / "history.csv"
            if not history.is_file() or history.stat().st_size <= 0:
                raise SU2BridgeError("SU2 RANS generated no non-empty history.csv")
            iterations = parse_history_iterations(history)
            su2_manifest["history_path"] = str(history.resolve())
            su2_manifest["history_sha256"] = _sha256(history)
            su2_manifest["iterations"] = iterations
            if iterations != max_iterations:
                raise SU2BridgeError(
                    f"SU2 RANS history has {iterations} iterations, expected {max_iterations}"
                )
            restart_candidates = sorted(su2_directory.glob("rans_solution_restart*.dat"))
            if len(restart_candidates) != 1 or restart_candidates[0].stat().st_size <= 0:
                raise SU2BridgeError("SU2 RANS generated no unique non-empty restart")
            su2_manifest["restart_file"] = str(restart_candidates[0].resolve())
            su2_manifest["restart_sha256"] = _sha256(restart_candidates[0])
            visualization = select_new_visualization(
                su2_directory,
                visualization_before,
                solution_prefix="rans_solution",
            )
            if visualization is None:
                raise SU2BridgeError("SU2 RANS generated no new rans_solution visualization")
            validation = validate_visualization_file(
                visualization,
                expected_points=int(mesh_validation["npoin"]),
                expected_cells=int(mesh_validation["nelem"]),
            )
            required_arrays = set(normalized["output"]["required_point_arrays"])
            actual_arrays = set(validation.get("float_arrays", ()))
            missing_arrays = sorted(required_arrays - actual_arrays)
            if missing_arrays:
                raise SU2BridgeError(
                    "SU2 RANS visualization lacks required point arrays: "
                    + ", ".join(missing_arrays)
                )
            surface_visualization = select_new_visualization(
                su2_directory,
                visualization_before,
                solution_prefix="rans_surface",
            )
            if surface_visualization is None:
                raise SU2BridgeError("SU2 RANS generated no new rans_surface visualization")
            surface_validation = validate_visualization_file(surface_visualization)
            su2_manifest["visualization_file"] = str(visualization)
            su2_manifest["visualization_validation"] = validation
            su2_manifest["surface_visualization_file"] = str(surface_visualization)
            su2_manifest["surface_visualization_validation"] = surface_validation
            su2_manifest["status"] = "PASS"
            su2_manifest["end_time"] = _utc_now()
            _write_json(run_manifest_path, su2_manifest)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, CommandExecutionError):
                result = error.result
            if result is not None:
                _apply_result(su2_manifest, result)
            su2_manifest["status"] = "FAIL"
            su2_manifest["end_time"] = _utc_now()
            su2_manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                "stderr": None if result is None else result.stderr,
            }
            _write_json(run_manifest_path, su2_manifest)
            report["su2"] = su2_manifest
            raise
        report["su2"] = su2_manifest
        report["startup_gate"] = "PASS"

        paraview_directory = output / "paraview"
        paraview_directory.mkdir()
        visualization_path = Path(str(su2_manifest["visualization_file"])).resolve(strict=True)
        history_path = Path(str(su2_manifest["history_path"])).resolve(strict=True)
        diagnostic_path = paraview_directory / "rans_diagnostics.json"
        contract_path = paraview_directory / "diagnostic_contract.json"
        freestream_velocity = float(selected_case["mach"]) * atmosphere.speed_of_sound_m_s
        contract = {
            "schema": "cfdpipe.rans_diagnostic_contract.v1",
            "run_scope": {
                "spatial_order": spatial_order,
                "restart_used": restart_source is not None,
                "diagnostic_only": True,
                "production_eligible": False,
            },
            "inputs": {
                "solution": {"path": str(visualization_path), "sha256": _sha256(visualization_path)},
                "mesh": {"path": str(local_mesh), "sha256": _sha256(local_mesh)},
                "history": {"path": str(history_path), "sha256": _sha256(history_path)},
            },
            "mesh": {
                "point_count": int(mesh_validation["npoin"]),
                "cell_count": int(mesh_validation["nelem"]),
            },
            "markers": {
                "farfield": farfield,
                "wall": wall,
                "rear_outlets": list(rear_outlets),
            },
            "yplus": {
                "target": float(normalized["yplus"]["target"]),
                "maximum": float(normalized["yplus"]["maximum"]),
            },
            "freestream": {
                "mach": float(selected_case["mach"]),
                "static_pressure_pa": atmosphere.static_pressure_pa,
                "static_temperature_k": atmosphere.static_temperature_k,
                "density_kg_m3": atmosphere.density_kg_m3,
                "velocity_m_s": freestream_velocity,
            },
            "gas": {"specific_heat_ratio": gamma},
            "reference": {
                "area_m2": area,
                "length_m": length,
                "moment_origin_m": list(origin),
            },
            "measurement": {
                "name": measurement["name"],
                "plane_coordinate_m": float(geometry["plane_coordinate_m"]),
                "normal_unit": list(geometry["normal_unit"]),
                "bounds_m": list(geometry["bounds_m"]),
                "area_m2": float(geometry["area_m2"]),
                "maximum_relative_area_error": float(
                    normalized["measurement_gate"]["maximum_relative_area_error"]
                ),
                "minimum_selected_cell_count": int(
                    normalized["measurement_gate"]["minimum_selected_cell_count"]
                ),
            },
            "provenance": {
                "project_sha256": inputs.input_records["project"]["sha256"],
                "markers_sha256": inputs.input_records["markers"]["sha256"],
                "su2_run_manifest_sha256": _sha256(run_manifest_path),
            },
        }
        _write_json(contract_path, contract)
        pv_bridge = ParaViewBridge(
            CommandRunner(
                paraview_directory,
                live_output=live_output,
                allow_mnt_c_executables=toolchain.allow_mnt_c_executables,
            ),
            pvbatch_tool.path,
            script_dir=paraview_script_directory,
            pvbatch_source=pvbatch_tool.source,
        )
        script_path = (
            Path(paraview_script_directory).resolve(strict=True)
            / "rans_smoke_diagnostics.py"
        ).resolve(strict=True)
        pv_result: CommandResult | None = None
        pv_error: ParaViewBridgeError | None = None
        diagnostics: dict[str, Any] | None = None
        paraview_manifest: dict[str, Any] = {
            "status": "FAIL",
            "pvbatch_path": str(Path(pvbatch_tool.path).resolve(strict=True)),
            "script_path": str(script_path),
            "script_sha256": _sha256(script_path),
            "diagnostic_contract_path": str(contract_path),
            "diagnostic_contract_sha256": _sha256(contract_path),
            "diagnostics_path": str(diagnostic_path),
            "diagnostics_sha256": None,
            "command": None,
            "diagnostics": None,
            "error": None,
        }
        try:
            try:
                pv_result = pv_bridge.run_pvbatch(
                    "rans_smoke_diagnostics.py",
                    (
                        "--input",
                        str(visualization_path),
                        "--mesh",
                        str(local_mesh),
                        "--history",
                        str(history_path),
                        "--contract",
                        str(contract_path),
                        "--output",
                        str(diagnostic_path),
                    ),
                    paraview_directory,
                    timeout,
                    live_output=live_output,
                    log_stem="pvbatch.rans",
                )
            except ParaViewBridgeError as error:
                pv_error = error
                pv_result = error.result
            if pv_result is not None:
                paraview_manifest["command"] = _command_record(pv_result)
            if diagnostic_path.is_file():
                diagnostics = _load_json(
                    diagnostic_path, label="RANS pvbatch diagnostics"
                )
                paraview_manifest["diagnostics_sha256"] = _sha256(diagnostic_path)
                paraview_manifest["diagnostics"] = diagnostics
                report["yplus_gate"] = diagnostics.get("yplus", {}).get(
                    "status", "FAIL"
                )
            if pv_error is not None:
                raise RANSSmokePipelineError(
                    f"pvbatch RANS diagnostics returned {pv_result.returncode}: "
                    f"{pv_error}"
                ) from pv_error
            if pv_result is None or pv_result.returncode != 0:
                raise RANSSmokePipelineError(
                    "pvbatch RANS diagnostics has no successful command result"
                )
            if diagnostics is None:
                raise RANSSmokePipelineError("pvbatch produced no diagnostics JSON")
            if diagnostics.get("status") != "PASS" or report["yplus_gate"] != "PASS":
                raise RANSSmokePipelineError(
                    "actual RANS/pvbatch y+ diagnostic gate failed"
                )
            paraview_manifest["status"] = "PASS"
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            paraview_manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "stderr": None if pv_result is None else pv_result.stderr,
            }
            report["paraview"] = paraview_manifest
            _write_json(
                paraview_directory / "paraview_manifest.json", paraview_manifest
            )
            raise
        report["paraview"] = paraview_manifest
        _write_json(paraview_directory / "paraview_manifest.json", paraview_manifest)

        source_after = {name: _sha256(path) for name, path in tracked_sources.items()}
        if source_after != source_before:
            raise RANSSmokePipelineError("a source or frozen evidence file changed during run")
        report["source_hashes_before"] = source_before
        report["source_hashes_after"] = source_after
        report["sources_unchanged"] = True
        report["status"] = "PASS"
        report["ended_at"] = _utc_now()
        _write_json(report_path, report)
        return report
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        report["status"] = "FAIL"
        report["ended_at"] = _utc_now()
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
            "stderr": getattr(getattr(error, "result", None), "stderr", None),
        }
        if source_before:
            report["source_hashes_before"] = source_before
            try:
                source_after = {
                    name: _sha256(path) for name, path in tracked_sources.items()
                }
            except OSError as source_error:
                report["source_hashes_after_error"] = str(source_error)
            else:
                report["source_hashes_after"] = source_after
                report["sources_unchanged"] = source_after == source_before
        _write_json(report_path, report)
        if isinstance(error, RANSSmokePipelineError):
            raise
        raise RANSSmokePipelineError(str(error)) from error


__all__ = ["RANSSmokePipelineError", "run_rans_smoke_validation"]
