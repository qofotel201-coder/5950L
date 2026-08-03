"""Bounded real-project outlet validation pipeline.

This stage consumes an already validated topology-smoke mesh.  It never runs
Gmsh, never changes CAD/config inputs, and never promotes its Euler evidence
to a production boundary decision.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import traceback
from typing import Any, Mapping, Sequence

from .atmosphere import us_standard_atmosphere_1976
from .bridges.paraview_bridge import ParaViewBridge
from .bridges.su2_bridge import (
    SU2Bridge,
    prepare_project_outlet_diagnostic_case,
)
from .outlet_diagnostics import parse_topology_smoke_su2
from .pipeline import load_real_connection_inputs
from .process import CommandRunner
from .toolchain import Toolchain


class OutletValidationPipelineError(RuntimeError):
    """Raised after a truthful outlet validation manifest is persisted."""


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
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OutletValidationPipelineError(f"invalid {label}: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise OutletValidationPipelineError(f"{label} root must be a JSON object")
    return payload


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


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
        raise OutletValidationPipelineError(
            f"role {role_text!r} must map to exactly one solver marker"
        )
    return matches[0]


def _finite_number(value: object, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise OutletValidationPipelineError(f"{label} must be a finite number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise OutletValidationPipelineError(f"{label} must be a finite number") from error
    if not math.isfinite(numeric) or (positive and numeric <= 0.0):
        raise OutletValidationPipelineError(f"{label} must be a finite number")
    return numeric


def _history_summary(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise OutletValidationPipelineError("history.csv has no data rows")
    fields = [name for name in (rows[0].keys() if rows else ()) if name]
    normalized_fields = {
        name: name.strip().strip('"').strip() for name in fields
    }
    residual_fields = [
        name
        for name, normalized in normalized_fields.items()
        if normalized.lower().startswith("rms[")
    ]
    if not residual_fields:
        raise OutletValidationPipelineError("history.csv has no RMS residual fields")
    residuals: dict[str, dict[str, float]] = {}
    for field in residual_fields:
        values = [_finite_number(row.get(field), label=f"history {field}") for row in rows]
        residuals[normalized_fields[field]] = {
            "initial": values[0],
            "final": values[-1],
            "final_minus_initial": values[-1] - values[0],
            "minimum": min(values),
            "maximum": max(values),
        }
    return {
        "row_count": len(rows),
        "residuals": residuals,
        "convergence_claimed": False,
    }


def run_outlet_validation(
    *,
    step_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str],
    cases_path: str | os.PathLike[str],
    markers_path: str | os.PathLike[str],
    topology_smoke_path: str | os.PathLike[str],
    case_id: str,
    source_mesh_path: str | os.PathLike[str],
    mesh_manifest_path: str | os.PathLike[str],
    output_directory: str | os.PathLike[str],
    trusted_repository_root: str | os.PathLike[str],
    toolchain: Toolchain,
    paraview_script_directory: str | os.PathLike[str],
    max_iterations: int = 200,
    timeout_seconds: float = 300.0,
    live_output: bool = True,
    controller_argv: Sequence[str] = (),
) -> dict[str, Any]:
    """Run SU2 and pvbatch for the next physical evidence gate only."""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if not 20 <= max_iterations <= 500:
        raise ValueError("max_iterations must be between 20 and 500")
    timeout = _finite_number(timeout_seconds, label="timeout_seconds", positive=True)
    repository_root = Path(trusted_repository_root).expanduser().resolve(strict=True)
    allowed_output_root = (repository_root / "runs" / "outlet_validation").resolve()
    raw_output = Path(output_directory).expanduser()
    if ".." in raw_output.parts:
        raise ValueError("outlet validation output must not contain '..'")
    output = Path(os.path.abspath(raw_output)).resolve(strict=False)
    if output == allowed_output_root or not _within(output, allowed_output_root):
        raise ValueError(
            f"output must be a dedicated child directory of {allowed_output_root}"
        )
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"outlet validation refuses to overwrite non-empty output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "outlet_validation_manifest.json"
    report: dict[str, Any] = {
        "status": "FAIL",
        "diagnostic_gate": "FAIL",
        "production_boundary_frozen": False,
        "production_eligible": False,
        "started_at": _utc_now(),
        "ended_at": None,
        "controller_argv": list(controller_argv),
        "inputs": {},
        "tools": {},
        "preparation": None,
        "su2": None,
        "paraview": None,
        "criteria": {},
        "boundary_recommendation": "NO_RECOMMENDATION",
        "next_quality_gate": (
            "qualified small boundary-layer mesh and repeated stable solution before freeze"
        ),
        "error": None,
    }
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
        selected_case = next(
            (case for case in inputs.cases if case.get("case_id") == case_id), None
        )
        if selected_case is None:
            raise OutletValidationPipelineError(f"case {case_id!r} is missing")
        production = inputs.project.get("production")
        if not isinstance(production, Mapping) or production.get("design_case") != case_id:
            raise OutletValidationPipelineError("outlet validation is limited to design_case")

        units = inputs.project.get("units")
        atmosphere_config = inputs.project.get("atmosphere")
        reference = inputs.project.get("reference")
        boundaries = inputs.project.get("boundaries")
        if not all(
            isinstance(value, Mapping)
            for value in (units, atmosphere_config, reference, boundaries)
        ):
            raise OutletValidationPipelineError(
                "project needs units, atmosphere, reference, and boundaries tables"
            )
        if units.get("altitude_input") != "km":
            raise OutletValidationPipelineError("outlet validation requires altitude_input=km")
        atmosphere = us_standard_atmosphere_1976(
            1000.0 * _finite_number(selected_case["altitude_km"], label="altitude_km"),
            model=str(atmosphere_config.get("model", "")),
            altitude_kind=str(atmosphere_config.get("altitude_kind", "")),
            viscosity_model=str(atmosphere_config.get("viscosity_model", "")),
        )
        area = _finite_number(reference.get("area_m2"), label="reference.area_m2", positive=True)
        length = _finite_number(
            reference.get("length_m"), label="reference.length_m", positive=True
        )
        origin_raw = reference.get("moment_origin_m")
        if (
            not isinstance(origin_raw, Sequence)
            or isinstance(origin_raw, (str, bytes))
            or len(origin_raw) != 3
        ):
            raise OutletValidationPipelineError("reference.moment_origin_m must have 3 values")
        origin = tuple(
            _finite_number(value, label="reference.moment_origin_m") for value in origin_raw
        )
        rear_roles = boundaries.get("rear_outlet_roles")
        if (
            not isinstance(rear_roles, Sequence)
            or isinstance(rear_roles, (str, bytes))
            or len(rear_roles) != 2
        ):
            raise OutletValidationPipelineError("project requires exactly two rear outlets")
        farfield_marker = _marker_for_role(inputs.markers, boundaries.get("farfield_role"))
        wall_marker = _marker_for_role(inputs.markers, boundaries.get("wall_role"))
        rear_markers = tuple(_marker_for_role(inputs.markers, role) for role in rear_roles)

        mesh = Path(source_mesh_path).expanduser().resolve(strict=True)
        mesh_manifest_file = Path(mesh_manifest_path).expanduser().resolve(strict=True)
        runs_root = (repository_root / "runs").resolve(strict=True)
        if not _within(mesh, runs_root) or not _within(mesh_manifest_file, runs_root):
            raise OutletValidationPipelineError("mesh evidence must stay under repository runs")
        mesh_manifest = _load_json(mesh_manifest_file, label="mesh manifest")
        if (
            mesh_manifest.get("status") != "PASS"
            or Path(str(mesh_manifest.get("path", ""))).resolve(strict=True) != mesh
            or mesh_manifest.get("sha256") != _sha256(mesh)
            or mesh_manifest.get("ndime") != 3
            or int(mesh_manifest.get("nelem", 0)) > 300_000
        ):
            raise OutletValidationPipelineError("mesh does not match bounded PASS evidence")
        parsed_mesh = parse_topology_smoke_su2(mesh)
        if parsed_mesh.sha256 != mesh_manifest["sha256"]:
            raise OutletValidationPipelineError("independent mesh parser hash mismatch")
        if set(parsed_mesh.markers) != {farfield_marker, wall_marker, *rear_markers}:
            raise OutletValidationPipelineError("mesh marker set differs from project mapping")

        su2_tool = toolchain.resolve("su2_cfd")
        pvbatch_tool = toolchain.resolve("pvbatch")
        report["tools"] = {
            "su2_cfd": su2_tool.as_dict(),
            "pvbatch": pvbatch_tool.as_dict(),
        }
        report["inputs"] = {
            "project": inputs.input_records["project"],
            "cases": inputs.input_records["cases"],
            "markers": inputs.input_records["markers"],
            "topology_smoke": inputs.input_records["topology_smoke_config"],
            "source_step": inputs.input_records["step"],
            "selected_case": dict(selected_case),
            "mesh_manifest": {
                "path": str(mesh_manifest_file),
                "sha256": _sha256(mesh_manifest_file),
            },
            "mesh": {"path": str(mesh), "sha256": _sha256(mesh)},
        }

        su2_directory = output / "su2"
        preparation = prepare_project_outlet_diagnostic_case(
            su2_tool.path,
            mesh,
            su2_directory,
            selected_case,
            atmosphere,
            reference_area_m2=area,
            reference_length_m=length,
            moment_origin_m=origin,
            farfield_marker=farfield_marker,
            wall_marker=wall_marker,
            rear_outlet_markers=rear_markers,
            max_iterations=max_iterations,
            mesh_validation=mesh_manifest,
        )
        report["preparation"] = preparation
        _write_json(su2_directory / "preparation_manifest.json", preparation)

        su2_bridge = SU2Bridge(
            CommandRunner(
                su2_directory,
                live_output=live_output,
                allow_mnt_c_executables=toolchain.allow_mnt_c_executables,
            ),
            su2_tool.path,
        )
        su2_manifest = su2_bridge.run_project_outlet_diagnostic_case(
            preparation,
            mesh_manifest,
            max_iterations=max_iterations,
            timeout_seconds=timeout,
            live_output=live_output,
        )
        history_path = Path(str(su2_manifest["history_path"])).resolve(strict=True)
        su2_manifest["history_summary"] = _history_summary(history_path)
        _write_json(su2_directory / "run_manifest.json", su2_manifest)
        report["su2"] = su2_manifest

        paraview_directory = output / "paraview"
        paraview_directory.mkdir(parents=True, exist_ok=True)
        diagnostics_path = paraview_directory / "outlet_diagnostics.json"
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
        pv_result = pv_bridge.run_pvbatch(
            "outlet_diagnostics.py",
            (
                "--input",
                str(su2_manifest["visualization_file"]),
                "--mesh",
                str(su2_directory / "mesh.su2"),
                "--output",
                str(diagnostics_path),
                "--outlet-marker",
                rear_markers[0],
                "--outlet-marker",
                rear_markers[1],
                "--farfield-marker",
                farfield_marker,
                "--wall-marker",
                wall_marker,
            ),
            paraview_directory,
            timeout,
            live_output=live_output,
            log_stem="pvbatch.outlet",
        )
        diagnostics = _load_json(diagnostics_path, label="outlet diagnostics")
        if (
            diagnostics.get("status") != "PASS"
            or diagnostics.get("computation_status") != "PASS"
            or diagnostics.get("boundary_decision") != "NOT_FROZEN"
            or diagnostics.get("production_eligible") is not False
        ):
            raise OutletValidationPipelineError("pvbatch outlet diagnostics contract failed")
        solution_record = diagnostics.get("input_solution")
        if (
            not isinstance(solution_record, Mapping)
            or solution_record.get("sha256")
            != su2_manifest["visualization_validation"]["sha256"]
        ):
            raise OutletValidationPipelineError("pvbatch input hash differs from SU2 manifest")

        marker_results = diagnostics.get("markers")
        mass_balance = diagnostics.get("mass_balance")
        if not isinstance(marker_results, Mapping) or not isinstance(mass_balance, Mapping):
            raise OutletValidationPipelineError("diagnostic JSON lacks marker/mass evidence")
        criteria: dict[str, bool] = {
            "su2_return_code_zero": su2_manifest.get("return_code") == 0,
            "requested_iterations_completed": su2_manifest.get("iterations")
            == max_iterations,
            "freestream_direction_verified": float(
                su2_manifest["freestream_log_evidence"][
                    "maximum_unit_vector_component_error"
                ]
            )
            <= 1.0e-4,
            "freestream_state_verified": max(
                float(
                    su2_manifest["freestream_log_evidence"][
                        "relative_static_pressure_error"
                    ]
                ),
                float(
                    su2_manifest["freestream_log_evidence"][
                        "relative_static_temperature_error"
                    ]
                ),
            )
            <= 1.0e-4,
            "all_outlet_samples_supersonic": all(
                float(marker_results[name]["normal_mach_vertex_min"]) > 1.0
                for name in rear_markers
            ),
            "no_outlet_backflow": all(
                float(marker_results[name]["backflow_area_fraction"]) == 0.0
                for name in rear_markers
            ),
            "global_mass_imbalance_below_one_percent": float(
                mass_balance["relative_global_imbalance"]
            )
            <= 0.01,
            "pvbatch_return_code_zero": pv_result.returncode == 0,
        }
        report["criteria"] = criteria
        report["paraview"] = {
            "status": "PASS",
            "pvbatch_path": str(pvbatch_tool.path),
            "command": list(pv_result.command),
            "cwd": pv_result.cwd,
            "return_code": pv_result.returncode,
            "start_time": pv_result.start_time,
            "end_time": pv_result.end_time,
            "stdout_log": str(pv_result.stdout_log),
            "stderr_log": str(pv_result.stderr_log),
            "command_log": str(pv_result.metadata_log),
            "script_path": str(
                Path(paraview_script_directory).resolve(strict=True)
                / "outlet_diagnostics.py"
            ),
            "script_sha256": _sha256(
                Path(paraview_script_directory).resolve(strict=True)
                / "outlet_diagnostics.py"
            ),
            "diagnostics_path": str(diagnostics_path),
            "diagnostics_sha256": _sha256(diagnostics_path),
            "diagnostics": diagnostics,
        }
        if not all(criteria.values()):
            failed = sorted(name for name, passed in criteria.items() if not passed)
            raise OutletValidationPipelineError(
                "outlet diagnostic criteria failed: " + ", ".join(failed)
            )
        report["status"] = "PASS"
        report["diagnostic_gate"] = "PASS"
        report["boundary_recommendation"] = (
            "PROVISIONAL_SUPERSONIC_SUPPORTED_ON_TOPOLOGY_SMOKE_ONLY"
        )
        report["ended_at"] = _utc_now()
        _write_json(manifest_path, report)
        return report
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        report["status"] = "FAIL"
        report["diagnostic_gate"] = "FAIL"
        report["ended_at"] = _utc_now()
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
            "stderr": getattr(getattr(error, "result", None), "stderr", None),
        }
        _write_json(manifest_path, report)
        if isinstance(error, OutletValidationPipelineError):
            raise
        raise OutletValidationPipelineError(str(error)) from error
