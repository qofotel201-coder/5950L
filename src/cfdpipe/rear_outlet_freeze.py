"""Evidence-only gate for freezing the rear-outlet boundary mode.

This module deliberately performs no solver or mesher work.  It evaluates an
explicit, hash-pinned TOML contract and the JSON evidence named by that
contract.  Evidence deficiencies are data-gate failures: they are accumulated
in the returned report instead of being raised as exceptions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping


CONTRACT_SCHEMA = "cfdpipe.rear_outlet_freeze.v1"
REPORT_SCHEMA = "cfdpipe.rear_outlet_freeze_report.v1"
RANS_REPORT_SCHEMA = "cfdpipe.rans_smoke_report.v1"

_ROOT_KEYS = {
    "schema",
    "status",
    "scope",
    "provenance",
    "boundary",
    "criteria",
    "evidence",
}
_SCOPE_KEYS = {"case_id", "production_eligible"}
_PROVENANCE_KEYS = {
    "source_step_sha256",
    "project_sha256",
    "cases_sha256",
    "markers_sha256",
    "topology_smoke_sha256",
    "euler_mesh_sha256",
    "rans_mesh_sha256",
}
_BOUNDARY_KEYS = {
    "rear_outlet_markers",
    "mode",
    "su2_option",
    "allow_static_pressure",
    "allow_back_pressure",
    "boundary_mode_frozen",
}
_CRITERIA_KEYS = {
    "sonic_normal_mach",
    "minimum_vertex_normal_mach_margin",
    "minimum_face_centroid_normal_mach_margin",
    "maximum_relative_global_mass_imbalance",
    "maximum_relative_drift_vertex_normal_mach",
    "maximum_relative_drift_face_centroid_normal_mach",
    "maximum_relative_drift_area_weighted_normal_mach",
    "maximum_relative_drift_net_mass_flow",
    "require_zero_subsonic_area_fraction",
    "require_zero_nonpositive_normal_mach_area_fraction",
    "require_zero_backflow_area_fraction",
    "require_zero_reverse_mass_flow",
}
_EVIDENCE_KEYS = {"records"}
_RECORD_KEYS = {
    "id",
    "stage",
    "manifest_path",
    "manifest_sha256",
    "diagnostics_path",
    "diagnostics_sha256",
    "restart_from",
}
_STAGES = {
    "euler_supplement",
    "rans_first_order",
    "rans_second_order_restart",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


def _base_report(config_path: Path) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "FAIL",
        "boundary_mode_frozen": False,
        "production_eligible": False,
        "config": {
            "path": str(config_path),
            "sha256": None,
            "schema": None,
            "status": None,
        },
        "scope": {"case_id": None},
        "boundary": {},
        "criteria": {},
        "checks": {},
        "evidence": [],
        "aggregate": {
            "stage_counts": {
                "euler_supplement": 0,
                "rans_first_order": 0,
                "rans_second_order_restart": 0,
            },
            "rans_checkpoint_count": 0,
            "relative_drift": {},
        },
        "errors": [],
    }


def _error(
    report: dict[str, Any],
    code: str,
    message: str,
    *,
    evidence_id: str | None = None,
    field: str | None = None,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if evidence_id is not None:
        item["evidence_id"] = evidence_id
    if field is not None:
        item["field"] = field
    report["errors"].append(item)


def _set_check(report: dict[str, Any], name: str, passed: bool) -> None:
    report["checks"][name] = bool(passed)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_tree(value: Any, location: str = "$") -> list[str]:
    failures: list[str] = []
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return failures
    if isinstance(value, float):
        if not math.isfinite(value):
            failures.append(location)
        return failures
    if isinstance(value, int):
        return failures
    if isinstance(value, Mapping):
        for key, child in value.items():
            failures.extend(_finite_tree(child, f"{location}.{key}"))
        return failures
    if isinstance(value, list):
        for index, child in enumerate(value):
            failures.extend(_finite_tree(child, f"{location}[{index}]"))
    return failures


def _json_safe(value: Any) -> Any:
    """Return a JSON-safe copy; internal sentinels are never report data."""

    if value is _MISSING:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _at(value: Any, *parts: str) -> Any:
    current = value
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _exact_keys(
    report: dict[str, Any],
    value: Any,
    expected: set[str],
    location: str,
) -> bool:
    if not isinstance(value, Mapping):
        _error(
            report,
            "CONTRACT_TYPE",
            f"{location} must be a TOML table",
            field=location,
        )
        return False
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        _error(
            report,
            "CONTRACT_MISSING_KEYS",
            f"{location} is missing keys: {', '.join(missing)}",
            field=location,
        )
    if unknown:
        _error(
            report,
            "CONTRACT_UNKNOWN_KEYS",
            f"{location} has unknown keys: {', '.join(unknown)}",
            field=location,
        )
    return not missing and not unknown


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _resolve_declared_path(base: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = base / candidate
        return candidate.resolve()
    except (OSError, ValueError):
        return None


def _load_json_evidence(
    report: dict[str, Any],
    path: Path | None,
    expected_sha: Any,
    *,
    evidence_id: str,
    kind: str,
) -> tuple[Any | None, str | None]:
    if path is None:
        _error(
            report,
            "EVIDENCE_PATH_INVALID",
            f"{kind} path must be an explicit non-empty path",
            evidence_id=evidence_id,
            field=f"{kind}_path",
        )
        return None, None
    if not _valid_sha(expected_sha):
        _error(
            report,
            "EVIDENCE_HASH_INVALID",
            f"{kind} expected SHA256 must be 64 lowercase hexadecimal characters",
            evidence_id=evidence_id,
            field=f"{kind}_sha256",
        )
    try:
        is_file = path.is_file()
    except (OSError, ValueError):
        is_file = False
    if not is_file:
        _error(
            report,
            "EVIDENCE_FILE_MISSING",
            f"{kind} file does not exist: {path}",
            evidence_id=evidence_id,
            field=f"{kind}_path",
        )
        return None, None
    try:
        actual_sha = _sha256(path)
    except OSError as exc:
        _error(
            report,
            "EVIDENCE_READ_FAILED",
            f"cannot hash {kind} file {path}: {exc}",
            evidence_id=evidence_id,
        )
        return None, None
    if _valid_sha(expected_sha) and actual_sha != expected_sha:
        _error(
            report,
            "EVIDENCE_HASH_MISMATCH",
            f"{kind} SHA256 is {actual_sha}, expected {expected_sha}",
            evidence_id=evidence_id,
            field=f"{kind}_sha256",
        )
    try:
        text = path.read_text(encoding="utf-8-sig")

        def reject_constant(token: str) -> None:
            raise ValueError(f"non-finite JSON constant {token}")

        payload = json.loads(text, parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _error(
            report,
            "EVIDENCE_JSON_INVALID",
            f"cannot parse {kind} JSON {path}: {exc}",
            evidence_id=evidence_id,
        )
        return None, actual_sha
    nonfinite = _finite_tree(payload)
    if nonfinite:
        _error(
            report,
            "NONFINITE_VALUE",
            f"{kind} contains NaN or Inf at: {', '.join(nonfinite)}",
            evidence_id=evidence_id,
        )
    if not isinstance(payload, Mapping):
        _error(
            report,
            "EVIDENCE_ROOT_TYPE",
            f"{kind} JSON root must be an object",
            evidence_id=evidence_id,
        )
        return None, actual_sha
    return payload, actual_sha


def _expect_equal(
    report: dict[str, Any],
    evidence_id: str,
    actual: Any,
    expected: Any,
    field: str,
    *,
    code: str = "EVIDENCE_VALUE_MISMATCH",
) -> bool:
    passed = actual is not _MISSING and actual == expected
    if not passed:
        shown = "<missing>" if actual is _MISSING else repr(actual)
        _error(
            report,
            code,
            f"{field} is {shown}, expected {expected!r}",
            evidence_id=evidence_id,
            field=field,
        )
    return passed


def _expect_sha(
    report: dict[str, Any],
    evidence_id: str,
    actual: Any,
    expected: Any,
    field: str,
) -> bool:
    if not _valid_sha(actual):
        _error(
            report,
            "INPUT_HASH_INVALID",
            f"{field} is missing or is not a lowercase SHA256",
            evidence_id=evidence_id,
            field=field,
        )
        return False
    return _expect_equal(
        report,
        evidence_id,
        actual,
        expected,
        field,
        code="INPUT_HASH_MISMATCH",
    )


def _numeric(
    report: dict[str, Any],
    evidence_id: str,
    value: Any,
    field: str,
) -> float | None:
    if not _is_number(value) or not math.isfinite(float(value)):
        _error(
            report,
            "METRIC_INVALID",
            f"{field} must be a finite number",
            evidence_id=evidence_id,
            field=field,
        )
        return None
    return float(value)


def _validate_common_inputs(
    report: dict[str, Any],
    evidence_id: str,
    manifest: Mapping[str, Any],
    stage: str,
    scope: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    if stage == "euler_supplement":
        fields = {
            "case_id": _at(manifest, "inputs", "selected_case", "case_id"),
            "source_step_sha256": _at(manifest, "inputs", "source_step", "sha256"),
            "project_sha256": _at(manifest, "inputs", "project", "sha256"),
            "cases_sha256": _at(manifest, "inputs", "cases", "sha256"),
            "markers_sha256": _at(manifest, "inputs", "markers", "sha256"),
            "topology_smoke_sha256": _at(
                manifest, "inputs", "topology_smoke", "sha256"
            ),
            "mesh_sha256": _at(manifest, "inputs", "mesh", "sha256"),
        }
        expected_mesh = provenance.get("euler_mesh_sha256")
    else:
        fields = {
            "case_id": _at(manifest, "inputs", "selected_case", "case_id"),
            "source_step_sha256": _at(
                manifest, "inputs", "source_records", "step", "sha256"
            ),
            "project_sha256": _at(
                manifest, "inputs", "source_records", "project", "sha256"
            ),
            "cases_sha256": _at(
                manifest, "inputs", "source_records", "cases", "sha256"
            ),
            "markers_sha256": _at(
                manifest, "inputs", "source_records", "markers", "sha256"
            ),
            "topology_smoke_sha256": _at(
                manifest,
                "inputs",
                "source_records",
                "topology_smoke_config",
                "sha256",
            ),
            "mesh_sha256": _at(manifest, "inputs", "mesh", "sha256"),
        }
        expected_mesh = provenance.get("rans_mesh_sha256")
    _expect_equal(
        report,
        evidence_id,
        fields["case_id"],
        scope.get("case_id"),
        "inputs.selected_case.case_id",
        code="CASE_ID_MISMATCH",
    )
    for name in (
        "source_step_sha256",
        "project_sha256",
        "cases_sha256",
        "markers_sha256",
        "topology_smoke_sha256",
    ):
        _expect_sha(report, evidence_id, fields[name], provenance.get(name), name)
    _expect_sha(report, evidence_id, fields["mesh_sha256"], expected_mesh, "mesh_sha256")


def _validate_manifest(
    report: dict[str, Any],
    evidence_id: str,
    stage: str,
    manifest: Mapping[str, Any],
    diagnostics_sha: str | None,
    scope: Mapping[str, Any],
    provenance: Mapping[str, Any],
    boundary: Mapping[str, Any],
) -> str | None:
    _validate_common_inputs(
        report, evidence_id, manifest, stage, scope, provenance
    )
    _expect_equal(report, evidence_id, manifest.get("status", _MISSING), "PASS", "status")
    _expect_equal(
        report,
        evidence_id,
        manifest.get("production_eligible", _MISSING),
        False,
        "production_eligible",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(manifest, "su2", "status"),
        "PASS",
        "su2.status",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(manifest, "su2", "return_code"),
        0,
        "su2.return_code",
    )
    if diagnostics_sha is not None:
        _expect_equal(
            report,
            evidence_id,
            _at(manifest, "paraview", "diagnostics_sha256"),
            diagnostics_sha,
            "paraview.diagnostics_sha256",
            code="DIAGNOSTICS_LINK_MISMATCH",
        )

    if stage == "euler_supplement":
        _expect_equal(
            report,
            evidence_id,
            manifest.get("diagnostic_gate", _MISSING),
            "PASS",
            "diagnostic_gate",
        )
        _expect_equal(
            report,
            evidence_id,
            _at(manifest, "preparation", "markers", "rear_outlets"),
            boundary.get("rear_outlet_markers"),
            "preparation.markers.rear_outlets",
            code="MARKER_MISMATCH",
        )
        _expect_equal(
            report,
            evidence_id,
            _at(manifest, "preparation", "scope", "outlet_pressure_or_backpressure_set"),
            False,
            "preparation.scope.outlet_pressure_or_backpressure_set",
        )
        return None

    _expect_equal(
        report,
        evidence_id,
        manifest.get("schema", _MISSING),
        RANS_REPORT_SCHEMA,
        "schema",
    )
    _expect_equal(
        report,
        evidence_id,
        manifest.get("startup_gate", _MISSING),
        "PASS",
        "startup_gate",
    )
    _expect_equal(
        report,
        evidence_id,
        manifest.get("yplus_gate", _MISSING),
        "PASS",
        "yplus_gate",
    )
    expected_order = 1 if stage == "rans_first_order" else 2
    _expect_equal(
        report,
        evidence_id,
        _at(manifest, "inputs", "rans_config", "normalized", "numerics", "spatial_order"),
        expected_order,
        "inputs.rans_config.normalized.numerics.spatial_order",
        code="RANS_ORDER_MISMATCH",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(
            manifest,
            "inputs",
            "rans_config",
            "normalized",
            "boundary",
            "rear_outlet_roles",
        ),
        boundary.get("rear_outlet_markers"),
        "inputs.rans_config.normalized.boundary.rear_outlet_roles",
        code="MARKER_MISMATCH",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(
            manifest,
            "inputs",
            "rans_config",
            "normalized",
            "boundary",
            "su2_rear_outlet_option",
        ),
        boundary.get("su2_option"),
        "inputs.rans_config.normalized.boundary.su2_rear_outlet_option",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(
            manifest,
            "inputs",
            "rans_config",
            "normalized",
            "boundary",
            "allow_static_outlet_pressure",
        ),
        boundary.get("allow_static_pressure"),
        "inputs.rans_config.normalized.boundary.allow_static_outlet_pressure",
    )
    _expect_equal(
        report,
        evidence_id,
        _at(
            manifest,
            "inputs",
            "rans_config",
            "normalized",
            "boundary",
            "allow_back_pressure",
        ),
        boundary.get("allow_back_pressure"),
        "inputs.rans_config.normalized.boundary.allow_back_pressure",
    )
    restart_sha = _at(manifest, "su2", "restart_sha256")
    if not _valid_sha(restart_sha):
        _error(
            report,
            "RESTART_OUTPUT_HASH_INVALID",
            "su2.restart_sha256 is missing or invalid",
            evidence_id=evidence_id,
            field="su2.restart_sha256",
        )
        return None
    if stage == "rans_second_order_restart":
        _expect_equal(
            report,
            evidence_id,
            manifest.get("spatial_order", _MISSING),
            2,
            "spatial_order",
            code="RANS_ORDER_MISMATCH",
        )
        _expect_equal(
            report,
            evidence_id,
            manifest.get("restart_used", _MISSING),
            True,
            "restart_used",
            code="RESTART_LINEAGE_MISMATCH",
        )
    return restart_sha


def _validate_diagnostics(
    report: dict[str, Any],
    evidence_id: str,
    stage: str,
    diagnostics: Mapping[str, Any],
    provenance: Mapping[str, Any],
    boundary: Mapping[str, Any],
    criteria: Mapping[str, Any],
) -> dict[str, Any] | None:
    _expect_equal(
        report,
        evidence_id,
        diagnostics.get("status", _MISSING),
        "PASS",
        "diagnostics.status",
    )
    _expect_equal(
        report,
        evidence_id,
        diagnostics.get("production_eligible", _MISSING),
        False,
        "diagnostics.production_eligible",
    )
    if stage == "euler_supplement":
        payload: Any = diagnostics
    else:
        _expect_equal(
            report,
            evidence_id,
            diagnostics.get("diagnostic_only", _MISSING),
            True,
            "diagnostics.diagnostic_only",
        )
        _expect_equal(
            report,
            evidence_id,
            _at(diagnostics, "yplus", "status"),
            "PASS",
            "diagnostics.yplus.status",
        )
        payload = diagnostics.get("outlets_and_mass_balance", _MISSING)
    if not isinstance(payload, Mapping):
        _error(
            report,
            "OUTLET_DIAGNOSTICS_MISSING",
            "outlet diagnostic object is missing",
            evidence_id=evidence_id,
        )
        return None

    _expect_equal(
        report,
        evidence_id,
        payload.get("computation_status", _MISSING),
        "PASS",
        "outlet.computation_status",
    )
    expected_mesh = (
        provenance.get("euler_mesh_sha256")
        if stage == "euler_supplement"
        else provenance.get("rans_mesh_sha256")
    )
    _expect_sha(
        report,
        evidence_id,
        _at(payload, "mesh", "sha256"),
        expected_mesh,
        "outlet.mesh.sha256",
    )
    volume_count_key = (
        "tetrahedron_count" if stage == "euler_supplement" else "volume_element_count"
    )
    normalized_mesh: dict[str, int | None] = {}
    for field in ("point_count", volume_count_key, "marker_count"):
        value = _at(payload, "mesh", field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            _error(
                report,
                "MESH_METADATA_INVALID",
                f"outlet.mesh.{field} must be a positive integer",
                evidence_id=evidence_id,
                field=f"outlet.mesh.{field}",
            )
            normalized_mesh[field] = None
        else:
            normalized_mesh[field] = value

    topology = payload.get("topology", _MISSING)
    if not isinstance(topology, Mapping):
        _error(
            report,
            "TOPOLOGY_MISSING",
            "outlet.topology must be an object",
            evidence_id=evidence_id,
        )
    else:
        _expect_equal(
            report,
            evidence_id,
            topology.get("coverage_complete", _MISSING),
            True,
            "outlet.topology.coverage_complete",
            code="TOPOLOGY_INVALID",
        )
        _expect_equal(
            report,
            evidence_id,
            topology.get("owner_errors", _MISSING),
            0,
            "outlet.topology.owner_errors",
            code="TOPOLOGY_INVALID",
        )
        _expect_equal(
            report,
            evidence_id,
            topology.get("degenerate_faces", _MISSING),
            0,
            "outlet.topology.degenerate_faces",
            code="TOPOLOGY_INVALID",
        )
        exterior = topology.get("exterior_face_count", _MISSING)
        marker_faces = topology.get("marker_face_count", _MISSING)
        if (
            not isinstance(exterior, int)
            or isinstance(exterior, bool)
            or exterior <= 0
            or marker_faces != exterior
        ):
            _error(
                report,
                "TOPOLOGY_INVALID",
                "marker_face_count must equal a positive exterior_face_count",
                evidence_id=evidence_id,
                field="outlet.topology.marker_face_count",
            )

    expected_markers = boundary.get("rear_outlet_markers")
    actual_outlets = payload.get("outlet_markers", _MISSING)
    outlet_names_valid = (
        isinstance(actual_outlets, list)
        and all(isinstance(item, str) for item in actual_outlets)
        and set(actual_outlets) == set(expected_markers or [])
    )
    if not outlet_names_valid:
        _error(
            report,
            "MARKER_MISMATCH",
            f"outlet_markers is {actual_outlets!r}, expected exactly {expected_markers!r}",
            evidence_id=evidence_id,
            field="outlet.outlet_markers",
        )
    marker_data = payload.get("markers", _MISSING)
    if not isinstance(marker_data, Mapping):
        _error(
            report,
            "MARKER_DATA_MISSING",
            "outlet.markers must be an object",
            evidence_id=evidence_id,
        )
        return None

    checkpoint: dict[str, Any] = {
        "mesh": {
            "point_count": normalized_mesh.get("point_count"),
            "volume_element_count": normalized_mesh.get(volume_count_key),
            "marker_count": normalized_mesh.get("marker_count"),
            "source_volume_count_key": volume_count_key,
        },
        "topology": dict(topology) if isinstance(topology, Mapping) else {},
        "markers": {},
        "mass_balance": {},
    }
    sonic = float(criteria["sonic_normal_mach"])
    min_vertex_margin = float(criteria["minimum_vertex_normal_mach_margin"])
    min_face_margin = float(criteria["minimum_face_centroid_normal_mach_margin"])
    zero_requirements = {
        "subsonic_normal_area_fraction": "require_zero_subsonic_area_fraction",
        "nonpositive_normal_mach_area_fraction": "require_zero_nonpositive_normal_mach_area_fraction",
        "backflow_area_fraction": "require_zero_backflow_area_fraction",
        "reverse_mass_flow_kg_s": "require_zero_reverse_mass_flow",
    }
    for marker in expected_markers or []:
        values = marker_data.get(marker, _MISSING)
        if not isinstance(values, Mapping):
            _error(
                report,
                "MARKER_DATA_MISSING",
                f"no diagnostics found for marker {marker}",
                evidence_id=evidence_id,
                field=f"outlet.markers.{marker}",
            )
            continue
        if stage != "euler_supplement":
            _expect_equal(
                report,
                evidence_id,
                values.get("normal_mach_evaluated", _MISSING),
                True,
                f"outlet.markers.{marker}.normal_mach_evaluated",
            )
        face_count = values.get("face_count", _MISSING)
        area = _numeric(
            report, evidence_id, values.get("area_m2", _MISSING), f"{marker}.area_m2"
        )
        if not isinstance(face_count, int) or isinstance(face_count, bool) or face_count <= 0:
            _error(
                report,
                "MARKER_TOPOLOGY_INVALID",
                f"{marker}.face_count must be a positive integer",
                evidence_id=evidence_id,
                field=f"{marker}.face_count",
            )
        if area is not None and area <= 0.0:
            _error(
                report,
                "MARKER_TOPOLOGY_INVALID",
                f"{marker}.area_m2 must be positive",
                evidence_id=evidence_id,
                field=f"{marker}.area_m2",
            )
        vertex_min = _numeric(
            report,
            evidence_id,
            values.get("normal_mach_vertex_min", _MISSING),
            f"{marker}.normal_mach_vertex_min",
        )
        face_min = _numeric(
            report,
            evidence_id,
            values.get("normal_mach_face_centroid_min", _MISSING),
            f"{marker}.normal_mach_face_centroid_min",
        )
        vertex_max = _numeric(
            report,
            evidence_id,
            values.get("normal_mach_vertex_max", _MISSING),
            f"{marker}.normal_mach_vertex_max",
        )
        face_max = _numeric(
            report,
            evidence_id,
            values.get("normal_mach_face_centroid_max", _MISSING),
            f"{marker}.normal_mach_face_centroid_max",
        )
        area_weighted_mean = _numeric(
            report,
            evidence_id,
            values.get("normal_mach_area_weighted_mean", _MISSING),
            f"{marker}.normal_mach_area_weighted_mean",
        )
        net_mass = _numeric(
            report,
            evidence_id,
            values.get("net_mass_flow_kg_s", _MISSING),
            f"{marker}.net_mass_flow_kg_s",
        )
        vertex_margin = None if vertex_min is None else vertex_min - sonic
        face_margin = None if face_min is None else face_min - sonic
        if vertex_min is not None and vertex_max is not None and vertex_max < vertex_min:
            _error(
                report,
                "NORMAL_MACH_RANGE_INVALID",
                f"{marker} vertex maximum is below its minimum",
                evidence_id=evidence_id,
                field=f"{marker}.normal_mach_vertex_max",
            )
        if face_min is not None and face_max is not None and face_max < face_min:
            _error(
                report,
                "NORMAL_MACH_RANGE_INVALID",
                f"{marker} face-centroid maximum is below its minimum",
                evidence_id=evidence_id,
                field=f"{marker}.normal_mach_face_centroid_max",
            )
        if vertex_margin is not None and vertex_margin < min_vertex_margin:
            _error(
                report,
                "NORMAL_MACH_MARGIN_FAILED",
                f"{marker} vertex margin {vertex_margin} is below {min_vertex_margin}",
                evidence_id=evidence_id,
                field=f"{marker}.normal_mach_vertex_min",
            )
        if face_margin is not None and face_margin < min_face_margin:
            _error(
                report,
                "NORMAL_MACH_MARGIN_FAILED",
                f"{marker} face-centroid margin {face_margin} is below {min_face_margin}",
                evidence_id=evidence_id,
                field=f"{marker}.normal_mach_face_centroid_min",
            )
        if net_mass is not None and net_mass <= 0.0:
            _error(
                report,
                "OUTLET_FLOW_DIRECTION_FAILED",
                f"{marker} net mass flow must be positive outward",
                evidence_id=evidence_id,
                field=f"{marker}.net_mass_flow_kg_s",
            )
        zero_values: dict[str, float | None] = {}
        for field, requirement in zero_requirements.items():
            number = _numeric(
                report,
                evidence_id,
                values.get(field, _MISSING),
                f"{marker}.{field}",
            )
            zero_values[field] = number
            if criteria.get(requirement) is True and number is not None and number != 0.0:
                _error(
                    report,
                    "ZERO_FLOW_CRITERION_FAILED",
                    f"{marker}.{field} must be exactly zero, got {number}",
                    evidence_id=evidence_id,
                    field=f"{marker}.{field}",
                )
        checkpoint["markers"][marker] = {
            "face_count": face_count if isinstance(face_count, int) else None,
            "area_m2": area,
            "normal_mach_vertex_min": vertex_min,
            "normal_mach_vertex_max": vertex_max,
            "normal_mach_face_centroid_min": face_min,
            "normal_mach_face_centroid_max": face_max,
            "normal_mach_area_weighted_mean": area_weighted_mean,
            "vertex_normal_mach_margin": vertex_margin,
            "face_centroid_normal_mach_margin": face_margin,
            "net_mass_flow_kg_s": net_mass,
            **zero_values,
        }

    imbalance = _numeric(
        report,
        evidence_id,
        _at(payload, "mass_balance", "relative_global_imbalance"),
        "mass_balance.relative_global_imbalance",
    )
    maximum_imbalance = float(criteria["maximum_relative_global_mass_imbalance"])
    if imbalance is not None:
        checkpoint["mass_balance"]["relative_global_imbalance"] = imbalance
        if imbalance < 0.0 or imbalance > maximum_imbalance:
            _error(
                report,
                "MASS_BALANCE_FAILED",
                f"relative global imbalance {imbalance} exceeds {maximum_imbalance}",
                evidence_id=evidence_id,
                field="mass_balance.relative_global_imbalance",
            )
    return checkpoint


def _relative_span(values: list[float]) -> float:
    denominator = max(abs(value) for value in values)
    if denominator == 0.0:
        return 0.0 if max(values) == min(values) else math.inf
    return (max(values) - min(values)) / denominator


def _validate_rans_consistency_and_drift(
    report: dict[str, Any],
    loaded_records: list[dict[str, Any]],
    boundary: Mapping[str, Any],
    criteria: Mapping[str, Any],
) -> None:
    rans = [
        item
        for item in loaded_records
        if item.get("stage") in {"rans_first_order", "rans_second_order_restart"}
        and isinstance(item.get("checkpoint"), Mapping)
    ]
    report["aggregate"]["rans_checkpoint_count"] = len(rans)
    if not rans:
        _set_check(report, "rans_mesh_topology_consistent", False)
        _set_check(report, "rans_relative_drift", False)
        return

    baseline = rans[0]["checkpoint"]
    consistent = True
    for item in rans[1:]:
        checkpoint = item["checkpoint"]
        evidence_id = item["id"]
        if checkpoint.get("mesh") != baseline.get("mesh"):
            consistent = False
            _error(
                report,
                "RANS_MESH_METADATA_DRIFT",
                "RANS mesh metadata differs across checkpoints",
                evidence_id=evidence_id,
            )
        if checkpoint.get("topology") != baseline.get("topology"):
            consistent = False
            _error(
                report,
                "RANS_TOPOLOGY_DRIFT",
                "RANS topology metadata differs across checkpoints",
                evidence_id=evidence_id,
            )
        for marker in boundary.get("rear_outlet_markers", []):
            current_marker = _at(checkpoint, "markers", marker)
            baseline_marker = _at(baseline, "markers", marker)
            if not isinstance(current_marker, Mapping) or not isinstance(
                baseline_marker, Mapping
            ):
                consistent = False
                continue
            for field in ("face_count", "area_m2"):
                if current_marker.get(field) != baseline_marker.get(field):
                    consistent = False
                    _error(
                        report,
                        "RANS_MARKER_TOPOLOGY_DRIFT",
                        f"{marker}.{field} differs across RANS checkpoints",
                        evidence_id=evidence_id,
                        field=f"{marker}.{field}",
                    )
    _set_check(report, "rans_mesh_topology_consistent", consistent)

    metric_limits = {
        "normal_mach_vertex_min": "maximum_relative_drift_vertex_normal_mach",
        "normal_mach_face_centroid_min": "maximum_relative_drift_face_centroid_normal_mach",
        "normal_mach_area_weighted_mean": "maximum_relative_drift_area_weighted_normal_mach",
        "net_mass_flow_kg_s": "maximum_relative_drift_net_mass_flow",
    }
    all_drift_passed = len(rans) >= 3
    if len(rans) < 3:
        _error(
            report,
            "RANS_DRIFT_EVIDENCE_INCOMPLETE",
            "relative drift requires at least three valid RANS checkpoints",
        )
    for marker in boundary.get("rear_outlet_markers", []):
        marker_result: dict[str, Any] = {}
        for metric, limit_name in metric_limits.items():
            values: list[float] = []
            for item in rans:
                value = _at(item.get("checkpoint"), "markers", marker, metric)
                if _is_number(value) and math.isfinite(float(value)):
                    values.append(float(value))
            if len(values) != len(rans) or not values:
                all_drift_passed = False
                marker_result[metric] = {
                    "status": "FAIL",
                    "values": values,
                    "relative_drift": None,
                    "maximum_allowed": criteria.get(limit_name),
                }
                continue
            drift = _relative_span(values)
            limit = float(criteria[limit_name])
            passed = math.isfinite(drift) and drift <= limit
            if not passed:
                all_drift_passed = False
                _error(
                    report,
                    "RANS_RELATIVE_DRIFT_FAILED",
                    f"{marker}.{metric} relative drift {drift} exceeds {limit}",
                    field=f"{marker}.{metric}",
                )
            marker_result[metric] = {
                "status": "PASS" if passed else "FAIL",
                "values": values,
                "relative_drift": drift if math.isfinite(drift) else None,
                "maximum_allowed": limit,
            }
        report["aggregate"]["relative_drift"][marker] = marker_result
    _set_check(report, "rans_relative_drift", all_drift_passed)


def _validate_contract(
    report: dict[str, Any], contract: Any
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], list[Any]] | None:
    if not isinstance(contract, Mapping):
        _error(report, "CONTRACT_ROOT_TYPE", "TOML root must be a table")
        return None
    _exact_keys(report, contract, _ROOT_KEYS, "root")
    _expect_equal(
        report, "contract", contract.get("schema", _MISSING), CONTRACT_SCHEMA, "schema"
    )
    _expect_equal(
        report,
        "contract",
        contract.get("status", _MISSING),
        "FROZEN_FOR_DESIGN_CASE_ONLY",
        "status",
    )
    report["config"]["schema"] = contract.get("schema")
    report["config"]["status"] = contract.get("status")

    scope = contract.get("scope", {})
    provenance = contract.get("provenance", {})
    boundary = contract.get("boundary", {})
    criteria = contract.get("criteria", {})
    evidence = contract.get("evidence", {})
    _exact_keys(report, scope, _SCOPE_KEYS, "scope")
    _exact_keys(report, provenance, _PROVENANCE_KEYS, "provenance")
    _exact_keys(report, boundary, _BOUNDARY_KEYS, "boundary")
    _exact_keys(report, criteria, _CRITERIA_KEYS, "criteria")
    _exact_keys(report, evidence, _EVIDENCE_KEYS, "evidence")

    scope_map = scope if isinstance(scope, Mapping) else {}
    provenance_map = provenance if isinstance(provenance, Mapping) else {}
    boundary_map = boundary if isinstance(boundary, Mapping) else {}
    criteria_map = criteria if isinstance(criteria, Mapping) else {}
    evidence_map = evidence if isinstance(evidence, Mapping) else {}

    contract_values_usable = True
    case_id = scope_map.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        contract_values_usable = False
        _error(report, "CONTRACT_VALUE_INVALID", "scope.case_id must be non-empty")
    report["scope"]["case_id"] = case_id
    if scope_map.get("production_eligible") is not False:
        contract_values_usable = False
        _error(
            report,
            "PRODUCTION_SCOPE_FORBIDDEN",
            "scope.production_eligible must be false",
            field="scope.production_eligible",
        )

    for key in _PROVENANCE_KEYS:
        if not _valid_sha(provenance_map.get(key)):
            contract_values_usable = False
            _error(
                report,
                "CONTRACT_HASH_INVALID",
                f"provenance.{key} must be a lowercase SHA256",
                field=f"provenance.{key}",
            )

    markers = boundary_map.get("rear_outlet_markers")
    if (
        not isinstance(markers, list)
        or len(markers) != 2
        or any(not isinstance(marker, str) or not marker.strip() for marker in markers)
        or len(set(markers)) != 2
    ):
        contract_values_usable = False
        _error(
            report,
            "CONTRACT_MARKERS_INVALID",
            "boundary.rear_outlet_markers must contain two distinct names",
        )
    for key in ("mode", "su2_option"):
        if not isinstance(boundary_map.get(key), str) or not boundary_map.get(key).strip():
            contract_values_usable = False
            _error(
                report,
                "CONTRACT_VALUE_INVALID",
                f"boundary.{key} must be non-empty",
                field=f"boundary.{key}",
            )
    for key, expected in (
        ("allow_static_pressure", False),
        ("allow_back_pressure", False),
        ("boundary_mode_frozen", True),
    ):
        if boundary_map.get(key) is not expected:
            contract_values_usable = False
            _error(
                report,
                "CONTRACT_BOUNDARY_UNSAFE",
                f"boundary.{key} must be {str(expected).lower()}",
                field=f"boundary.{key}",
            )
    report["boundary"] = dict(boundary_map)

    numeric_criteria = {
        "sonic_normal_mach",
        "minimum_vertex_normal_mach_margin",
        "minimum_face_centroid_normal_mach_margin",
        "maximum_relative_global_mass_imbalance",
        "maximum_relative_drift_vertex_normal_mach",
        "maximum_relative_drift_face_centroid_normal_mach",
        "maximum_relative_drift_area_weighted_normal_mach",
        "maximum_relative_drift_net_mass_flow",
    }
    criteria_usable = isinstance(criteria, Mapping)
    for key in numeric_criteria:
        value = criteria_map.get(key)
        if not _is_number(value) or not math.isfinite(float(value)) or float(value) < 0.0:
            criteria_usable = False
            _error(
                report,
                "CONTRACT_CRITERION_INVALID",
                f"criteria.{key} must be a finite non-negative number",
                field=f"criteria.{key}",
            )
    for key in _CRITERIA_KEYS - numeric_criteria:
        if criteria_map.get(key) is not True:
            criteria_usable = False
            _error(
                report,
                "CONTRACT_CRITERION_INVALID",
                f"criteria.{key} must be true",
                field=f"criteria.{key}",
            )
    if _is_number(criteria_map.get("sonic_normal_mach")) and float(
        criteria_map["sonic_normal_mach"]
    ) <= 0.0:
        criteria_usable = False
        _error(
            report,
            "CONTRACT_CRITERION_INVALID",
            "criteria.sonic_normal_mach must be positive",
            field="criteria.sonic_normal_mach",
        )
    report["criteria"] = dict(criteria_map)

    records = evidence_map.get("records", [])
    if not isinstance(records, list):
        _error(report, "CONTRACT_RECORDS_INVALID", "evidence.records must be an array")
        records = []
    _set_check(report, "contract_structure", len(report["errors"]) == 0)
    if not (
        isinstance(scope, Mapping)
        and isinstance(provenance, Mapping)
        and isinstance(boundary, Mapping)
        and criteria_usable
        and contract_values_usable
    ):
        return None
    return scope_map, provenance_map, boundary_map, records


def _write_report(
    report: dict[str, Any], output_path: Path | None
) -> dict[str, Any]:
    report["status"] = "PASS" if not report["errors"] else "FAIL"
    report["boundary_mode_frozen"] = bool(
        report["status"] == "PASS"
        and report.get("boundary", {}).get("boundary_mode_frozen") is True
    )
    report["production_eligible"] = False
    sanitized = _json_safe(report)
    report.clear()
    report.update(sanitized)
    if output_path is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            _error(
                report,
                "OUTPUT_WRITE_FAILED",
                f"cannot write report {output_path}: {exc}",
            )
            report["status"] = "FAIL"
            report["boundary_mode_frozen"] = False
    return report


def evaluate_rear_outlet_freeze(
    config_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Evaluate hash-pinned outlet evidence without launching external tools.

    Malformed, missing, inconsistent, or physically insufficient evidence is
    represented by ``status=FAIL`` and a complete ``errors`` array.  ``TypeError``
    is reserved for callers passing values that are not path-like.
    """

    if not isinstance(config_path, (str, os.PathLike)):
        raise TypeError("config_path must be str or os.PathLike")
    if output_path is not None and not isinstance(output_path, (str, os.PathLike)):
        raise TypeError("output_path must be str, os.PathLike, or None")
    try:
        config = Path(config_path).resolve()
    except (OSError, ValueError):
        config = Path(os.fspath(config_path))
    try:
        output = Path(output_path).resolve() if output_path is not None else None
    except (OSError, ValueError):
        output = Path(os.fspath(output_path)) if output_path is not None else None
    report = _base_report(config)
    try:
        config_is_file = config.is_file()
    except (OSError, ValueError):
        config_is_file = False
    if not config_is_file:
        _error(report, "CONTRACT_FILE_MISSING", f"contract file does not exist: {config}")
        return _write_report(report, output)
    try:
        report["config"]["sha256"] = _sha256(config)
        contract = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        _error(report, "CONTRACT_READ_FAILED", f"cannot read TOML contract: {exc}")
        return _write_report(report, output)
    nonfinite = _finite_tree(contract)
    if nonfinite:
        _error(
            report,
            "NONFINITE_VALUE",
            f"contract contains NaN or Inf at: {', '.join(nonfinite)}",
        )
    validated = _validate_contract(report, contract)
    if validated is None:
        return _write_report(report, output)
    scope, provenance, boundary, records = validated
    criteria = contract["criteria"]

    stage_counts = {stage: 0 for stage in _STAGES}
    loaded: list[dict[str, Any]] = []
    ids: set[str] = set()
    base = config.parent
    for index, record in enumerate(records):
        record_location = f"evidence.records[{index}]"
        if not isinstance(record, Mapping):
            _error(
                report,
                "CONTRACT_RECORD_INVALID",
                f"{record_location} must be a table",
                field=record_location,
            )
            continue
        _exact_keys(report, record, _RECORD_KEYS, record_location)
        evidence_id = record.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            evidence_id = f"invalid-record-{index}"
            _error(
                report,
                "EVIDENCE_ID_INVALID",
                f"{record_location}.id must be non-empty",
                field=f"{record_location}.id",
            )
        elif evidence_id in ids:
            _error(
                report,
                "EVIDENCE_ID_DUPLICATE",
                f"duplicate evidence id {evidence_id}",
                evidence_id=evidence_id,
            )
        ids.add(evidence_id)
        stage = record.get("stage")
        if not isinstance(stage, str) or stage not in _STAGES:
            _error(
                report,
                "EVIDENCE_STAGE_INVALID",
                f"stage {stage!r} is not supported",
                evidence_id=evidence_id,
            )
        else:
            stage_counts[stage] += 1
        restart_from = record.get("restart_from")
        if not isinstance(restart_from, str):
            _error(
                report,
                "RESTART_REFERENCE_INVALID",
                "restart_from must be a string",
                evidence_id=evidence_id,
            )
        elif stage == "rans_second_order_restart" and not restart_from:
            _error(
                report,
                "RESTART_REFERENCE_INVALID",
                "second-order RANS evidence must name restart_from",
                evidence_id=evidence_id,
            )
        elif stage != "rans_second_order_restart" and restart_from:
            _error(
                report,
                "RESTART_REFERENCE_INVALID",
                "restart_from must be empty except for second-order RANS evidence",
                evidence_id=evidence_id,
            )

        manifest_path = _resolve_declared_path(base, record.get("manifest_path"))
        diagnostics_path = _resolve_declared_path(base, record.get("diagnostics_path"))
        before_errors = len(report["errors"])
        manifest, manifest_sha = _load_json_evidence(
            report,
            manifest_path,
            record.get("manifest_sha256"),
            evidence_id=evidence_id,
            kind="manifest",
        )
        diagnostics, diagnostics_sha = _load_json_evidence(
            report,
            diagnostics_path,
            record.get("diagnostics_sha256"),
            evidence_id=evidence_id,
            kind="diagnostics",
        )
        if manifest_path is not None and diagnostics_path == manifest_path:
            _error(
                report,
                "EVIDENCE_PATH_COLLISION",
                "manifest_path and diagnostics_path must identify different files",
                evidence_id=evidence_id,
            )
        item: dict[str, Any] = {
            "id": evidence_id,
            "stage": stage,
            "restart_from": restart_from,
            "manifest": {
                "path": str(manifest_path) if manifest_path else None,
                "expected_sha256": record.get("manifest_sha256"),
                "actual_sha256": manifest_sha,
            },
            "diagnostics": {
                "path": str(diagnostics_path) if diagnostics_path else None,
                "expected_sha256": record.get("diagnostics_sha256"),
                "actual_sha256": diagnostics_sha,
            },
            "checkpoint": None,
            "restart_output_sha256": None,
        }
        if isinstance(manifest, Mapping) and isinstance(stage, str) and stage in _STAGES:
            item["restart_output_sha256"] = _validate_manifest(
                report,
                evidence_id,
                stage,
                manifest,
                diagnostics_sha,
                scope,
                provenance,
                boundary,
            )
        if isinstance(diagnostics, Mapping) and isinstance(stage, str) and stage in _STAGES:
            item["checkpoint"] = _validate_diagnostics(
                report,
                evidence_id,
                stage,
                diagnostics,
                provenance,
                boundary,
                criteria,
            )
        item["status"] = "PASS" if len(report["errors"]) == before_errors else "FAIL"
        report["evidence"].append(item)
        item["_manifest_payload"] = manifest
        item["_manifest_index"] = index
        loaded.append(item)

    report["aggregate"]["stage_counts"] = stage_counts
    counts_pass = (
        stage_counts["euler_supplement"] == 1
        and stage_counts["rans_first_order"] >= 2
        and stage_counts["rans_second_order_restart"] >= 1
    )
    if not counts_pass:
        _error(
            report,
            "EVIDENCE_STAGE_COUNT_FAILED",
            "required evidence is exactly one Euler supplement, at least two first-order RANS checkpoints, and at least one second-order restart RANS checkpoint",
        )
    _set_check(report, "evidence_stage_counts", counts_pass)

    by_id = {item["id"]: item for item in loaded}
    lineage_pass = True
    for item in loaded:
        if item.get("stage") != "rans_second_order_restart":
            continue
        evidence_id = item["id"]
        source = by_id.get(item.get("restart_from"))
        manifest = item.get("_manifest_payload")
        if (
            source is None
            or source.get("stage") not in {"rans_first_order", "rans_second_order_restart"}
            or source.get("_manifest_index", -1) >= item.get("_manifest_index", -1)
        ):
            lineage_pass = False
            _error(
                report,
                "RESTART_LINEAGE_INVALID",
                "restart_from must reference an earlier RANS evidence record",
                evidence_id=evidence_id,
            )
            continue
        lineage = manifest.get("restart_lineage") if isinstance(manifest, Mapping) else None
        if not isinstance(lineage, Mapping):
            lineage_pass = False
            _error(
                report,
                "RESTART_LINEAGE_MISSING",
                "second-order manifest must contain restart_lineage",
                evidence_id=evidence_id,
            )
            continue
        checks = (
            _expect_equal(
                report,
                evidence_id,
                lineage.get("used", _MISSING),
                True,
                "restart_lineage.used",
            ),
            _expect_equal(
                report,
                evidence_id,
                lineage.get("source_manifest_sha256", _MISSING),
                source.get("manifest", {}).get("actual_sha256"),
                "restart_lineage.source_manifest_sha256",
                code="RESTART_LINEAGE_MISMATCH",
            ),
            _expect_equal(
                report,
                evidence_id,
                lineage.get("input_restart_sha256", _MISSING),
                source.get("restart_output_sha256"),
                "restart_lineage.input_restart_sha256",
                code="RESTART_LINEAGE_MISMATCH",
            ),
        )
        source_su2_manifest_sha = lineage.get("source_su2_manifest_sha256")
        if not _valid_sha(source_su2_manifest_sha):
            lineage_pass = False
            _error(
                report,
                "RESTART_LINEAGE_MISMATCH",
                "restart_lineage.source_su2_manifest_sha256 must be a lowercase SHA256",
                evidence_id=evidence_id,
                field="restart_lineage.source_su2_manifest_sha256",
            )
        lineage_pass = lineage_pass and all(checks)
    _set_check(report, "second_order_restart_lineage", lineage_pass)

    _validate_rans_consistency_and_drift(report, loaded, boundary, criteria)
    for item in loaded:
        item.pop("_manifest_payload", None)
        item.pop("_manifest_index", None)
        item["status"] = (
            "FAIL"
            if any(error.get("evidence_id") == item["id"] for error in report["errors"])
            else "PASS"
        )
    _set_check(report, "all_evidence_records", all(item["status"] == "PASS" for item in loaded))
    _set_check(report, "no_nan_or_inf", not any(error["code"] == "NONFINITE_VALUE" for error in report["errors"]))
    return _write_report(report, output)


__all__ = ["evaluate_rear_outlet_freeze"]
