"""In-memory, non-authorizing discovery of coarse-mesh repair signatures.

The strict calibration builder remains the only path that can produce a mesh.
This module deliberately stops after the real strategy has constructed and
audited its in-memory repair profile.  It records stable geometry identities,
Gmsh lifecycle evidence and source immutability, but it can never grant a
calibration PASS or production-mesh eligibility.
"""

from __future__ import annotations

import copy
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
import traceback
from typing import Any, Callable, Mapping

from .boundary_layer_smoke import (
    RealProjectBoundaryLayerStrategy,
    _reject_fatal_gmsh_log,
)
from .boundary_layer_schedule_binding import (
    BoundaryLayerScheduleBindingError,
    validate_boundary_layer_schedule_binding,
)
from .coarse_mesh import make_coarse_strategy_config
from .coarse_projection_evidence import (
    CoarseProjectionEvidenceError,
    validate_projection_evidence_record,
)
from .coarse_schedule_feasibility import (
    CoarseScheduleFeasibilityError,
    make_minimum_growth_endpoint,
    validate_minimum_growth_endpoint,
)
from .coarse_direction_feasibility import (
    CoarseDirectionFeasibilityError,
    ENDPOINT_SCHEMA as OWNER_FREE_DIRECTION_ENDPOINT_SCHEMA,
    PATTERN_ENDPOINT_SCHEMA as OWNER_FREE_PATTERN_ENDPOINT_SCHEMA,
    make_owner_free_direction_endpoint,
    make_owner_free_direction_pattern_endpoint,
    validate_owner_free_direction_endpoint,
)
from .coarse_component_direction import (
    CoarseComponentDirectionError,
    DISCOVERY_SCHEMA as COMPONENT_DIRECTION_DISCOVERY_SCHEMA,
    validate_component_direction_discovery,
)
from .coarse_direction_replay import (
    CoarseDirectionReplayError,
    HOMOTOPY_DISCOVERY_SCHEMA,
    PHYSICAL_DISCOVERY_SCHEMA,
    make_physical_schedule_homotopy_endpoint,
    validate_direction_replay_approval,
    validate_physical_schedule_homotopy_discovery,
    validate_physical_schedule_homotopy_endpoint,
    validate_physical_schedule_replay_discovery,
)
from .coarse_schedule_continuation import (
    CoarseScheduleContinuationError,
    DISCOVERY_SCHEMA as SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA,
    REQUEST as SCHEDULE_CONTINUATION_REQUEST,
    make_schedule_frontier_direction_endpoint,
    validate_schedule_frontier_direction_endpoint,
    validate_schedule_frontier_direction_continuation,
)


MANIFEST_SCHEMA = "cfdpipe.coarse_repair_audit_manifest.v1"
DISCOVERY_SCHEMA = "cfdpipe.coarse_repair_discovery.v1"
RESOURCE_ABORT_MANIFEST_SCHEMA = (
    "cfdpipe.coarse_repair_audit_resource_abort.v1"
)
RESOURCE_ABORT_MANIFEST_NAME = "resource_abort_manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PHYSICAL_HOMOTOPY_REQUEST = (
    "physical-schedule-fixed-direction-homotopy-audit"
)
PHYSICAL_HOMOTOPY_PURPOSE = (
    "physical_local_schedule_fixed_direction_homotopy"
)
SCHEDULE_CONTINUATION_PURPOSE = (
    "physical_schedule_first_frontier_direction_continuation"
)
_OBSERVED_COUNT_FIELDS = {
    "initial_bad_prism_count",
    "negative_volume_prism_count",
    "cone_node_count",
    "full_layer_bad_prism_count",
    "initial_below_threshold_prism_count",
    "repair_candidate_prism_count",
    "bad_source_triangle_count",
    "strict_bad_source_triangle_count",
    "preferred_strict_failure_root_count",
    "smoothing_group_count",
    "candidate_smoothed_root_count",
    "owner_ambiguity_count",
    "changed_smoothed_root_count",
    "source_prism_count",
    "projected_3d_element_count",
}


class CoarseRepairAuditError(RuntimeError):
    """Repair discovery could not produce trustworthy audit-only evidence."""


class _AuditOnlyGmshProxy:
    """Expose the Gmsh API while fail-closing every serialization attempt."""

    def __init__(self, gmsh: Any) -> None:
        self._gmsh = gmsh
        self.write_attempts: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gmsh, name)

    def write(self, path: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.write_attempts.append(str(path))
        raise CoarseRepairAuditError(
            "gmsh.write is forbidden during coarse repair discovery"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_canonical_utc_z(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    canonical = parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return (
        parsed.utcoffset() == timezone.utc.utcoffset(parsed)
        and value == canonical
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _is_read_only(path: Path) -> bool:
    return not bool(path.stat().st_mode & stat.S_IWUSR)


def _snapshot(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "read_only": _is_read_only(path),
    }


def _stable_file_record(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise CoarseRepairAuditError(f"{label} is not a regular non-link file")
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != before.st_size
    ):
        raise CoarseRepairAuditError(f"{label} changed while it was read")
    return payload, {
        "path": str(path.resolve(strict=True)),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _strict_json_mapping(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise CoarseRepairAuditError(f"{label} contains {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseRepairAuditError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise CoarseRepairAuditError(f"{label} must be a JSON object")
    return value


def _resource_abort_status_hex(returncode: int) -> str:
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise CoarseRepairAuditError("resource-abort return code is not an integer")
    if returncode < -(2**31) or returncode > 0xFFFFFFFF:
        raise CoarseRepairAuditError("resource-abort return code is outside 32 bits")
    return f"0x{returncode & 0xFFFFFFFF:08X}"


def _resource_abort_command_evidence(
    command_metadata_path: Path,
) -> dict[str, Any]:
    path = command_metadata_path.expanduser().resolve(strict=True)
    payload, identity = _stable_file_record(path, label="command metadata")
    metadata = _strict_json_mapping(payload, label="command metadata")
    required = {
        "returncode",
        "status_hex",
        "timed_out",
        "interrupted",
        "resource_aborted",
        "resource_abort_reason",
        "output_complete",
        "runner_error",
        "termination_method",
        "graceful_termination_attempted",
        "graceful_termination_succeeded",
        "hard_kill_attempted",
        "hard_kill_succeeded",
    }
    if not required <= set(metadata):
        raise CoarseRepairAuditError(
            "command metadata lacks the strict resource-abort termination fields"
        )
    returncode = metadata["returncode"]
    status_hex = _resource_abort_status_hex(returncode)
    boolean_fields = (
        "timed_out",
        "interrupted",
        "resource_aborted",
        "output_complete",
        "graceful_termination_attempted",
        "graceful_termination_succeeded",
        "hard_kill_attempted",
        "hard_kill_succeeded",
    )
    if any(type(metadata[field]) is not bool for field in boolean_fields):
        raise CoarseRepairAuditError(
            "command metadata resource-abort termination flags are not boolean"
        )
    reason = metadata["resource_abort_reason"]
    method = metadata["termination_method"]
    graceful_attempted = metadata["graceful_termination_attempted"]
    graceful_succeeded = metadata["graceful_termination_succeeded"]
    hard_attempted = metadata["hard_kill_attempted"]
    hard_succeeded = metadata["hard_kill_succeeded"]
    expected_method = (
        "resource_abort_hard_kill"
        if hard_succeeded
        else "resource_abort_graceful"
    )
    if (
        returncode == 0
        or metadata["status_hex"] != status_hex
        or metadata["resource_aborted"] is not True
        or metadata["timed_out"] is not False
        or metadata["interrupted"] is not False
        or metadata["output_complete"] is not True
        or metadata["runner_error"] is not None
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(method, str)
        or method != expected_method
        or graceful_attempted is not True
        or graceful_succeeded and not graceful_attempted
        or hard_succeeded and not hard_attempted
        or not (graceful_succeeded or hard_succeeded)
        or graceful_succeeded and (hard_attempted or hard_succeeded)
    ):
        raise CoarseRepairAuditError(
            "command metadata is not a complete controller resource abort"
        )
    return {
        **identity,
        "returncode": returncode,
        "status_hex": status_hex,
        "timed_out": False,
        "interrupted": False,
        "resource_aborted": True,
        "resource_abort_reason": reason,
        "output_complete": True,
        "runner_error": None,
        "termination_method": method,
        "graceful_termination_attempted": graceful_attempted,
        "graceful_termination_succeeded": graceful_succeeded,
        "hard_kill_attempted": hard_attempted,
        "hard_kill_succeeded": hard_succeeded,
    }


def _formal_manifest_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path.resolve(strict=False)),
            "exists": False,
            "state": "MISSING",
            "sha256": None,
            "size_bytes": None,
            "parse_error": None,
        }
    payload, identity = _stable_file_record(path, label="formal audit manifest")
    try:
        value = _strict_json_mapping(payload, label="formal audit manifest")
    except CoarseRepairAuditError as error:
        return {
            **identity,
            "exists": True,
            "state": "INCOMPLETE",
            "parse_error": f"{type(error).__name__}: {error}",
        }
    session = value.get("gmsh_session")
    source_after = value.get("source_after")
    complete = bool(
        value.get("schema") == MANIFEST_SCHEMA
        and value.get("status") in {"PASS", "INCOMPLETE", "FAIL"}
        and isinstance(value.get("ended_at_utc"), str)
        and bool(value["ended_at_utc"])
        and isinstance(session, Mapping)
        and type(session.get("finalize_attempted")) is bool
        and type(session.get("finalize_called")) is bool
        and isinstance(session.get("cleanup_errors"), list)
        and isinstance(source_after, Mapping)
        and _SHA256.fullmatch(str(source_after.get("sha256", ""))) is not None
        and type(source_after.get("size_bytes")) is int
        and type(source_after.get("read_only")) is bool
        and type(value.get("source_unchanged")) is bool
    )
    return {
        **identity,
        "exists": True,
        "state": "COMPLETE" if complete else "INCOMPLETE",
        "parse_error": None,
    }


def _resource_abort_output_inventory(output_directory: Path) -> dict[str, Any]:
    output = output_directory.expanduser().resolve(strict=False)
    records: list[dict[str, Any]] = []
    if output.exists():
        if not output.is_dir() or output.is_symlink():
            raise CoarseRepairAuditError(
                "resource-abort output is not a regular non-link directory"
            )
        output = output.resolve(strict=True)
        for candidate in sorted(output.rglob("*"), key=lambda item: item.as_posix()):
            if candidate.is_symlink():
                raise CoarseRepairAuditError(
                    "resource-abort output inventory contains a link"
                )
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if output not in resolved.parents:
                raise CoarseRepairAuditError(
                    "resource-abort output inventory escaped its directory"
                )
            _payload, identity = _stable_file_record(
                resolved, label="resource-abort output file"
            )
            records.append(
                {
                    "relative_path": resolved.relative_to(output).as_posix(),
                    "size_bytes": identity["size_bytes"],
                    "sha256": identity["sha256"],
                }
            )
    mesh_suffixes = {".msh", ".su2"}
    solver_suffixes = {".cfg", ".csv", ".dat", ".restart", ".rst"}
    paraview_suffixes = {".vtk", ".vtu", ".pvtu", ".vtm"}
    suffixes = [Path(record["relative_path"]).suffix.casefold() for record in records]
    mesh_count = sum(suffix in mesh_suffixes for suffix in suffixes)
    solver_count = sum(suffix in solver_suffixes for suffix in suffixes)
    paraview_count = sum(suffix in paraview_suffixes for suffix in suffixes)
    formal = _formal_manifest_state(output / "coarse_repair_audit_manifest.json")
    return {
        "path": str(output),
        "exists": output.exists(),
        "recursive_file_count": len(records),
        "recursive_files": records,
        "recursive_files_sha256": hashlib.sha256(
            json.dumps(
                records,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest(),
        "mesh_artifact_count": mesh_count,
        "su2_artifact_count": solver_count,
        "paraview_artifact_count": paraview_count,
        "downstream_artifact_count": solver_count + paraview_count,
        "formal_manifest": formal,
    }


def validate_coarse_repair_resource_abort_manifest(
    value: Any,
    *,
    expected_evidence_directory: Path,
    expected_command_metadata_path: Path,
    expected_source_brep_path: Path,
    expected_source_sha256: str,
    expected_output_directory: Path,
) -> dict[str, Any]:
    """Rebuild every mutable observation in one fail-closed abort record."""

    fields = {
        "schema",
        "status",
        "failure_code",
        "published_at_utc",
        "audit_only",
        "downstream_consumable",
        "calibration_PASS_authorized",
        "production_mesh_eligible",
        "command_evidence",
        "gmsh_lifecycle",
        "source_brep_after_abort",
        "output_inventory",
        "manifest_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CoarseRepairAuditError(
            "resource-abort manifest has an incomplete field set"
        )
    unsigned = dict(value)
    configured_hash = unsigned.pop("manifest_sha256", None)
    if (
        value.get("schema") != RESOURCE_ABORT_MANIFEST_SCHEMA
        or value.get("status") != "FAIL"
        or value.get("failure_code")
        != "WORKER_RESOURCE_ABORTED_WITHOUT_COMPLETE_MANIFEST"
        or not _is_canonical_utc_z(value.get("published_at_utc"))
        or value.get("audit_only") is not True
        or value.get("downstream_consumable") is not False
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or not isinstance(configured_hash, str)
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
    ):
        raise CoarseRepairAuditError(
            "resource-abort manifest status, policy, or hash is stale"
        )
    evidence = expected_evidence_directory.expanduser().resolve(strict=True)
    if not evidence.is_dir() or evidence.is_symlink():
        raise CoarseRepairAuditError("resource-abort evidence directory is invalid")
    command_path = expected_command_metadata_path.expanduser().resolve(strict=True)
    if command_path.parent != evidence:
        raise CoarseRepairAuditError(
            "resource-abort command metadata is outside its evidence directory"
        )
    command = _resource_abort_command_evidence(command_path)
    source = expected_source_brep_path.expanduser().resolve(strict=True)
    expected_sha256 = str(expected_source_sha256).casefold()
    if (
        _SHA256.fullmatch(expected_sha256) is None
        or not source.is_file()
        or source.is_symlink()
        or source.suffix.casefold() != ".brep"
    ):
        raise CoarseRepairAuditError("resource-abort source BREP identity is invalid")
    source_snapshot = _snapshot(source)
    source_evidence = {
        **source_snapshot,
        "expected_sha256": expected_sha256,
        "matches_expected_sha256": (
            source_snapshot["sha256"] == expected_sha256
        ),
    }
    output = _resource_abort_output_inventory(expected_output_directory)
    if output["formal_manifest"]["state"] == "COMPLETE":
        raise CoarseRepairAuditError(
            "complete formal audit manifest makes abort evidence redundant"
        )
    lifecycle = {
        "finalize_status": "UNKNOWN_NOT_PROVEN",
        "finalize_called": None,
        "output_capture_is_not_finalize_evidence": True,
    }
    if (
        value.get("command_evidence") != command
        or value.get("source_brep_after_abort") != source_evidence
        or value.get("output_inventory") != output
        or value.get("gmsh_lifecycle") != lifecycle
    ):
        raise CoarseRepairAuditError(
            "resource-abort manifest observations differ from current evidence"
        )
    return copy.deepcopy(dict(value))


def _atomic_write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_coarse_repair_resource_abort_manifest(
    *,
    evidence_directory: Path,
    command_metadata_path: Path,
    source_brep_path: Path,
    expected_source_sha256: str,
    output_directory: Path,
) -> dict[str, Any]:
    """Atomically publish independent FAIL evidence after a guarded abort."""

    evidence = evidence_directory.expanduser().resolve(strict=True)
    if not evidence.is_dir() or evidence.is_symlink():
        raise CoarseRepairAuditError("resource-abort evidence directory is invalid")
    target = evidence / RESOURCE_ABORT_MANIFEST_NAME
    if target.exists() or target.is_symlink():
        raise CoarseRepairAuditError("resource-abort manifest already exists")
    command = _resource_abort_command_evidence(command_metadata_path)
    source = source_brep_path.expanduser().resolve(strict=True)
    expected_sha256 = str(expected_source_sha256).casefold()
    if (
        _SHA256.fullmatch(expected_sha256) is None
        or not source.is_file()
        or source.is_symlink()
        or source.suffix.casefold() != ".brep"
    ):
        raise CoarseRepairAuditError("resource-abort source BREP identity is invalid")
    source_snapshot = _snapshot(source)
    output = _resource_abort_output_inventory(output_directory)
    if output["formal_manifest"]["state"] == "COMPLETE":
        raise CoarseRepairAuditError(
            "complete formal audit manifest makes abort evidence redundant"
        )
    unsigned = {
        "schema": RESOURCE_ABORT_MANIFEST_SCHEMA,
        "status": "FAIL",
        "failure_code": "WORKER_RESOURCE_ABORTED_WITHOUT_COMPLETE_MANIFEST",
        "published_at_utc": _utc_now(),
        "audit_only": True,
        "downstream_consumable": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "command_evidence": command,
        "gmsh_lifecycle": {
            "finalize_status": "UNKNOWN_NOT_PROVEN",
            "finalize_called": None,
            "output_capture_is_not_finalize_evidence": True,
        },
        "source_brep_after_abort": {
            **source_snapshot,
            "expected_sha256": expected_sha256,
            "matches_expected_sha256": (
                source_snapshot["sha256"] == expected_sha256
            ),
        },
        "output_inventory": output,
    }
    value = {**unsigned, "manifest_sha256": _canonical_sha256(unsigned)}
    validate_coarse_repair_resource_abort_manifest(
        value,
        expected_evidence_directory=evidence,
        expected_command_metadata_path=command_metadata_path,
        expected_source_brep_path=source,
        expected_source_sha256=expected_sha256,
        expected_output_directory=output_directory,
    )
    _atomic_write_new_json(target, value)
    payload, _identity = _stable_file_record(
        target, label="published resource-abort manifest"
    )
    saved = _strict_json_mapping(payload, label="published resource-abort manifest")
    return validate_coarse_repair_resource_abort_manifest(
        saved,
        expected_evidence_directory=evidence,
        expected_command_metadata_path=command_metadata_path,
        expected_source_brep_path=source,
        expected_source_sha256=expected_sha256,
        expected_output_directory=output_directory,
    )


def _strict_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoarseRepairAuditError(
                f"audit manifest contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def load_and_validate_physical_homotopy_audit_manifest(
    path: Path,
    expected_sha256: str,
    *,
    output_root: Path,
    expected_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_projection_evidence: Mapping[str, Any],
    expected_local_schedule_binding: Mapping[str, Any],
    expected_approval: Mapping[str, Any],
    expected_endpoint: Mapping[str, Any],
    expected_projected_3d_elements: int,
    expected_prism_element_count: int,
    expected_core_element_count: int,
    expected_layer_count: int,
    expected_surface_count: int,
    expected_surface_schedules: Mapping[str, Any],
    expected_minimum_prism_scaled_jacobian: float,
    expected_minimum_core_tetra_gamma: float,
) -> dict[str, Any]:
    """Load one attested homotopy audit and re-run its authoritative gate."""

    normalized_sha256 = str(expected_sha256).casefold()
    if _SHA256.fullmatch(normalized_sha256) is None:
        raise CoarseRepairAuditError(
            "homotopy audit SHA-256 must be lowercase hexadecimal"
        )
    resolved = path.expanduser().resolve(strict=True)
    root = output_root.expanduser().resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or resolved.name != "coarse_repair_audit_manifest.json"
        or resolved.parent.parent != root
        or _sha256_file(resolved) != normalized_sha256
    ):
        raise CoarseRepairAuditError(
            "homotopy audit manifest identity is invalid"
        )

    def reject_constant(value: str) -> None:
        raise CoarseRepairAuditError(
            f"homotopy audit manifest contains non-finite JSON value {value}"
        )

    try:
        raw = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=reject_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        CoarseRepairAuditError,
    ) as error:
        raise CoarseRepairAuditError(
            f"homotopy audit manifest cannot be read: {error}"
        ) from error
    if not isinstance(raw, Mapping):
        raise CoarseRepairAuditError(
            "homotopy audit manifest is not a JSON object"
        )
    selected = raw.get("characteristic_length_m")
    strategy = raw.get("strategy_config")
    if (
        raw.get("schema") != MANIFEST_SCHEMA
        or raw.get("status") != "INCOMPLETE"
        or raw.get("audit_only") is not True
        or raw.get("calibration_PASS_authorized") is not False
        or raw.get("production_mesh_eligible") is not False
        or raw.get("mesh_written") is not False
        or raw.get("su2_called") is not False
        or raw.get("paraview_called") is not False
        or raw.get("external_commands") != []
        or raw.get("error") is not None
        or raw.get("audit_purpose") != PHYSICAL_HOMOTOPY_PURPOSE
        or raw.get("schedule_feasibility_endpoint_requested")
        != PHYSICAL_HOMOTOPY_REQUEST
        or raw.get("coarse_contract_sha256")
        != expected_contract_sha256
        or isinstance(selected, bool)
        or not isinstance(selected, (int, float))
        or not math.isclose(
            float(selected),
            float(expected_characteristic_length_m),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
        or raw.get("projection_evidence")
        != dict(expected_projection_evidence)
        or raw.get("local_schedule_binding")
        != dict(expected_local_schedule_binding)
        or raw.get("direction_replay_approval")
        != dict(expected_approval)
        or raw.get("physical_schedule_homotopy_endpoint")
        != dict(expected_endpoint)
        or not isinstance(strategy, Mapping)
        or strategy.get("audit_purpose") != PHYSICAL_HOMOTOPY_PURPOSE
        or strategy.get("fixed_direction_replay_only") is not True
        or strategy.get("fixed_direction_homotopy_only") is not True
        or strategy.get("combined_endpoint_only") is not True
        or strategy.get("owner_free_direction_replay_approval")
        != dict(expected_approval)
        or strategy.get("schedule_feasibility_endpoint")
        != dict(expected_endpoint)
        or strategy.get("physical_schedule_homotopy_endpoint")
        != dict(expected_endpoint)
    ):
        raise CoarseRepairAuditError(
            "homotopy audit manifest lineage or safety scope is stale"
        )
    try:
        discovery = validate_physical_schedule_homotopy_discovery(
            raw.get("repair_discovery"),
            expected_endpoint=expected_endpoint,
            expected_approval=expected_approval,
            expected_projected_3d_elements=expected_projected_3d_elements,
            expected_prism_element_count=expected_prism_element_count,
            expected_core_element_count=expected_core_element_count,
            expected_layer_count=expected_layer_count,
            expected_surface_count=expected_surface_count,
            expected_surface_schedules=expected_surface_schedules,
            expected_minimum_prism_scaled_jacobian=(
                expected_minimum_prism_scaled_jacobian
            ),
            expected_minimum_core_tetra_gamma=(
                expected_minimum_core_tetra_gamma
            ),
        )
    except (CoarseDirectionReplayError, KeyError, TypeError, ValueError) as error:
        raise CoarseRepairAuditError(
            f"homotopy audit discovery is invalid: {error}"
        ) from error
    if (
        discovery.get("schema") != HOMOTOPY_DISCOVERY_SCHEMA
        or discovery.get("status") != "INCOMPLETE"
        or discovery.get("profile_complete") is not False
        or discovery.get("maximum_tested_pass_fraction_float_hex")
        != "0x0.0p+0"
        or discovery.get("first_tested_fail_fraction_float_hex")
        != "0x1.0000000000000p-12"
    ):
        raise CoarseRepairAuditError(
            "homotopy audit does not expose the required first frontier"
        )
    return {
        "path": str(resolved),
        "sha256": normalized_sha256,
        "size_bytes": resolved.stat().st_size,
        "manifest_status": "INCOMPLETE",
        "discovery": discovery,
    }


def _error_record(error: BaseException) -> dict[str, Any]:
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def make_coarse_repair_audit_strategy_config(
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    *,
    local_schedule_binding: Mapping[str, Any],
    schedule_feasibility_endpoint: str | None = None,
    direction_replay_approval: Mapping[str, Any] | None = None,
    homotopy_audit_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive an explicit audit-only strategy contract from the strict inputs."""

    result = make_coarse_strategy_config(
        contract,
        characteristic_length_m,
        local_schedule_binding=local_schedule_binding,
        expected_schedule_contract_sha256=str(
            local_schedule_binding.get("coarse_contract_sha256", "")
        ),
    )
    result.pop("normalized_config_sha256", None)
    result.update(
        {
            "coarse_contract_sha256": str(
                local_schedule_binding.get("coarse_contract_sha256", "")
            ),
            "smoke_only": False,
            "calibration_only": False,
            "repair_audit_only": True,
            "projection_only": False,
            "contract_mode": "coarse_repair_audit_only",
            "calibration_PASS_authorized": False,
            "production_mesh_eligible": False,
            "mesh_written": False,
            "su2_called": False,
            "paraview_called": False,
        }
    )
    homotopy_requested = (
        schedule_feasibility_endpoint == PHYSICAL_HOMOTOPY_REQUEST
    )
    continuation_requested = (
        schedule_feasibility_endpoint == SCHEDULE_CONTINUATION_REQUEST
    )
    combined_endpoint_requested = homotopy_requested or continuation_requested
    if (
        schedule_feasibility_endpoint is not None
        and direction_replay_approval is not None
        and not combined_endpoint_requested
    ):
        raise CoarseRepairAuditError(
            "minimum-growth search and physical-schedule replay are mutually exclusive"
        )
    if combined_endpoint_requested and direction_replay_approval is None:
        raise CoarseRepairAuditError(
            "physical-schedule combined audit requires fixed-direction approval"
        )
    if continuation_requested is not (homotopy_audit_evidence is not None):
        raise CoarseRepairAuditError(
            "schedule direction continuation requires one validated homotopy audit"
        )
    if direction_replay_approval is not None:
        source_quality = direction_replay_approval.get(
            "source_minimum_growth_final_quality"
        )
        quality = result.get("quality_improvement")
        if not isinstance(source_quality, Mapping) or not isinstance(quality, Mapping):
            raise CoarseRepairAuditError(
                "physical-schedule replay approval lacks quality lineage"
            )
        try:
            validated_replay = validate_direction_replay_approval(
                direction_replay_approval,
                expected_coarse_contract_sha256=str(
                    local_schedule_binding.get("coarse_contract_sha256", "")
                ),
                expected_characteristic_length_m=float(characteristic_length_m),
                expected_local_schedule_binding_sha256=str(
                    local_schedule_binding.get("binding_sha256", "")
                ),
                expected_projection_manifest_sha256=str(
                    direction_replay_approval.get(
                        "projection_manifest_sha256", ""
                    )
                ),
                expected_projected_3d_elements=(
                    int(source_quality["prism_element_count"])
                    + int(source_quality["core_element_count"])
                ),
                expected_minimum_prism_scaled_jacobian=float(
                    quality["minimum_prism_scaled_jacobian"]
                ),
                expected_minimum_core_tetra_gamma=float(
                    quality["minimum_core_tetra_gamma"]
                ),
            )
        except (
            CoarseDirectionReplayError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise CoarseRepairAuditError(
                f"physical-schedule replay approval is invalid: {error}"
            ) from error
        replay_fields: dict[str, Any] = {
            "audit_purpose": (
                SCHEDULE_CONTINUATION_PURPOSE
                if continuation_requested
                else (
                    PHYSICAL_HOMOTOPY_PURPOSE
                    if homotopy_requested
                    else "physical_local_schedule_fixed_direction_replay"
                )
            ),
            "fixed_direction_replay_only": True,
            "combined_endpoint_only": bool(combined_endpoint_requested),
            "owner_free_direction_replay_approval": validated_replay,
        }
        if combined_endpoint_requested:
            try:
                homotopy_endpoint = make_physical_schedule_homotopy_endpoint(
                    baseline_binding_sha256=str(
                        local_schedule_binding.get("binding_sha256", "")
                    ),
                    first_layer_height_m=float(result["first_layer_height_m"]),
                    layer_count=int(result["layer_count"]),
                )
                homotopy_endpoint = validate_physical_schedule_homotopy_endpoint(
                    homotopy_endpoint,
                    expected_binding_sha256=str(
                        local_schedule_binding.get("binding_sha256", "")
                    ),
                    expected_first_layer_height_m=float(
                        result["first_layer_height_m"]
                    ),
                    expected_layer_count=int(result["layer_count"]),
                )
            except (
                CoarseDirectionReplayError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise CoarseRepairAuditError(
                    f"physical-schedule homotopy endpoint is invalid: {error}"
                ) from error
            replay_fields.update(
                {
                    "schedule_feasibility_endpoint": homotopy_endpoint,
                    "physical_schedule_homotopy_endpoint": homotopy_endpoint,
                }
            )
            if homotopy_requested:
                replay_fields["fixed_direction_homotopy_only"] = True
            else:
                evidence = homotopy_audit_evidence
                discovery = (
                    evidence.get("discovery")
                    if isinstance(evidence, Mapping)
                    else None
                )
                manifest_sha256 = (
                    str(evidence.get("sha256", "")).casefold()
                    if isinstance(evidence, Mapping)
                    else ""
                )
                quality = result.get("quality_improvement")
                if not isinstance(discovery, Mapping) or not isinstance(
                    quality, Mapping
                ):
                    raise CoarseRepairAuditError(
                        "schedule direction continuation evidence is incomplete"
                    )
                endpoint_arguments = {
                    "homotopy_manifest_sha256": manifest_sha256,
                    "homotopy_discovery": discovery,
                    "homotopy_endpoint_sha256": str(
                        homotopy_endpoint["endpoint_sha256"]
                    ),
                    "direction_replay_approval_sha256": str(
                        validated_replay["approval_sha256"]
                    ),
                    "local_schedule_binding_sha256": str(
                        local_schedule_binding.get("binding_sha256", "")
                    ),
                    "minimum_prism_scaled_jacobian_for_pass": float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    "minimum_core_tetra_gamma_for_pass": float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                    "layer_count": int(result["layer_count"]),
                    "first_layer_height_m": float(
                        result["first_layer_height_m"]
                    ),
                }
                try:
                    continuation_endpoint = (
                        make_schedule_frontier_direction_endpoint(
                            **endpoint_arguments
                        )
                    )
                    continuation_endpoint = (
                        validate_schedule_frontier_direction_endpoint(
                            continuation_endpoint,
                            **endpoint_arguments,
                        )
                    )
                except (
                    CoarseScheduleContinuationError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise CoarseRepairAuditError(
                        "schedule direction continuation endpoint is invalid: "
                        f"{error}"
                    ) from error
                replay_fields.update(
                    {
                        "first_frontier_direction_continuation_only": True,
                        "schedule_direction_continuation_endpoint": (
                            continuation_endpoint
                        ),
                    }
                )
        result.update(replay_fields)
    elif schedule_feasibility_endpoint is not None:
        if schedule_feasibility_endpoint not in {
            "minimum-growth-existing-direction-repair",
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
        }:
            raise CoarseRepairAuditError(
                "unsupported schedule feasibility endpoint"
            )
        schedule_endpoint = make_minimum_growth_endpoint(
            baseline_binding_sha256=str(
                local_schedule_binding.get("binding_sha256", "")
            ),
            first_layer_height_m=float(result["first_layer_height_m"]),
            layer_count=int(result["layer_count"]),
        )
        result["schedule_feasibility_endpoint"] = schedule_endpoint
        if (
            schedule_feasibility_endpoint
            == "minimum-growth-existing-direction-repair"
        ):
            result["audit_purpose"] = (
                "minimum_growth_plus_existing_direction_repair"
            )
        else:
            result["audit_purpose"] = (
                "minimum_growth_owner_free_component_direction_audit"
                if schedule_feasibility_endpoint
                == "minimum-growth-owner-free-component-direction-audit"
                else "minimum_growth_owner_free_component_pattern_audit"
            )
            result["owner_free_direction_endpoint"] = (
                (
                    make_owner_free_direction_endpoint
                    if schedule_feasibility_endpoint
                    == "minimum-growth-owner-free-component-direction-audit"
                    else make_owner_free_direction_pattern_endpoint
                )(
                    schedule_endpoint_sha256=str(
                        schedule_endpoint["endpoint_sha256"]
                    )
                )
            )
        result["combined_endpoint_only"] = True
    result["normalized_config_sha256"] = _canonical_sha256(result)
    return result


def _validate_repair_audit_strategy_config(
    *,
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    strategy_config: Any,
    validated_binding: Mapping[str, Any],
    expected_schedule_feasibility_endpoint: str | None = None,
    expected_direction_replay_approval: Mapping[str, Any] | None = None,
    expected_homotopy_audit_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(strategy_config, dict):
        raise CoarseRepairAuditError("repair audit strategy config is not an object")
    configured_hash = strategy_config.get("normalized_config_sha256")
    unsigned = dict(strategy_config)
    unsigned.pop("normalized_config_sha256", None)
    if (
        not isinstance(configured_hash, str)
        or _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(unsigned) != configured_hash
    ):
        raise CoarseRepairAuditError("repair audit strategy config hash is stale")
    if (
        isinstance(contract.get("base_smoke_contract"), Mapping)
        and isinstance(contract.get("post_mesh_repair"), Mapping)
    ):
        expected_strategy = make_coarse_repair_audit_strategy_config(
            contract,
            characteristic_length_m,
            local_schedule_binding=validated_binding,
            schedule_feasibility_endpoint=expected_schedule_feasibility_endpoint,
            direction_replay_approval=expected_direction_replay_approval,
            homotopy_audit_evidence=expected_homotopy_audit_evidence,
        )
        if strategy_config != expected_strategy:
            raise CoarseRepairAuditError(
                "repair audit strategy differs from its deterministic trusted contract"
            )
    required_scope = {
        "smoke_only": False,
        "calibration_only": False,
        "repair_audit_only": True,
        "projection_only": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    if (
        strategy_config.get("schema") != "cfdpipe.coarse_mesh.v1"
        or strategy_config.get("status") != "CONFIGURED"
        or strategy_config.get("contract_mode") != "coarse_repair_audit_only"
        or any(
            strategy_config.get(name) is not expected
            for name, expected in required_scope.items()
        )
    ):
        raise CoarseRepairAuditError(
            "repair audit strategy config violates the exclusive audit-only mode matrix"
        )
    contract_hash = contract.get("normalized_config_sha256")
    if (
        strategy_config.get("coarse_contract_sha256") != contract_hash
        or strategy_config.get("local_boundary_layer_schedule")
        != dict(validated_binding)
    ):
        raise CoarseRepairAuditError(
            "repair audit strategy config binding lineage is stale"
        )
    configured_characteristic = strategy_config.get("characteristic_length_m")
    if isinstance(configured_characteristic, bool):
        raise CoarseRepairAuditError(
            "repair audit strategy characteristic length is invalid"
        )
    try:
        configured_characteristic = float(configured_characteristic)
    except (TypeError, ValueError) as error:
        raise CoarseRepairAuditError(
            "repair audit strategy characteristic length is invalid"
        ) from error
    if not math.isclose(
        configured_characteristic,
        characteristic_length_m,
        rel_tol=1.0e-12,
        abs_tol=0.0,
    ):
        raise CoarseRepairAuditError(
            "repair audit strategy characteristic length differs from the request"
        )
    design = contract.get("boundary_layer_design")
    contract_fingerprints = contract.get("wall_surface_fingerprints")
    if not isinstance(design, Mapping) or not isinstance(contract_fingerprints, list):
        raise CoarseRepairAuditError(
            "repair audit coarse contract lacks boundary-layer strategy lineage"
        )
    configured_fingerprints = strategy_config.get("wall_surface_fingerprints")
    if (
        not isinstance(configured_fingerprints, list)
        or [str(value).casefold() for value in configured_fingerprints]
        != [str(value).casefold() for value in contract_fingerprints]
    ):
        raise CoarseRepairAuditError(
            "repair audit strategy wall fingerprint lineage is stale"
        )
    if strategy_config.get("layer_count") != design.get("layer_count"):
        raise CoarseRepairAuditError(
            "repair audit strategy layer count differs from the coarse contract"
        )
    for config_field, design_field in (
        ("first_layer_height_m", "first_layer_height_m"),
        ("growth_ratio", "growth_ratio"),
    ):
        try:
            configured_value = float(strategy_config.get(config_field))
            contract_value = float(design.get(design_field))
        except (TypeError, ValueError) as error:
            raise CoarseRepairAuditError(
                f"repair audit strategy {config_field} lineage is invalid"
            ) from error
        if not math.isclose(
            configured_value,
            contract_value,
            rel_tol=1.0e-12,
            abs_tol=1.0e-14,
        ):
            raise CoarseRepairAuditError(
                f"repair audit strategy {config_field} lineage is stale"
            )
    raw_endpoint = strategy_config.get("schedule_feasibility_endpoint")
    if (
        expected_schedule_feasibility_endpoint is not None
        and expected_direction_replay_approval is not None
        and expected_schedule_feasibility_endpoint
        not in (PHYSICAL_HOMOTOPY_REQUEST, SCHEDULE_CONTINUATION_REQUEST)
    ):
        raise CoarseRepairAuditError(
            "repair audit search and replay expectations conflict"
        )
    if expected_direction_replay_approval is not None:
        homotopy_expected = (
            expected_schedule_feasibility_endpoint == PHYSICAL_HOMOTOPY_REQUEST
        )
        continuation_expected = (
            expected_schedule_feasibility_endpoint
            == SCHEDULE_CONTINUATION_REQUEST
        )
        combined_expected = homotopy_expected or continuation_expected
        expected_replay_purpose = (
            SCHEDULE_CONTINUATION_PURPOSE
            if continuation_expected
            else (
                PHYSICAL_HOMOTOPY_PURPOSE
                if homotopy_expected
                else "physical_local_schedule_fixed_direction_replay"
            )
        )
        if (
            (
                "schedule_feasibility_endpoint" in strategy_config
                and not combined_expected
            )
            or "owner_free_direction_endpoint" in strategy_config
            or strategy_config.get("audit_purpose")
            != expected_replay_purpose
            or strategy_config.get("fixed_direction_replay_only") is not True
            or strategy_config.get("combined_endpoint_only")
            is not bool(combined_expected)
            or strategy_config.get("owner_free_direction_replay_approval")
            != dict(expected_direction_replay_approval)
        ):
            raise CoarseRepairAuditError(
                "physical-schedule fixed-direction replay strategy is stale"
            )
        raw_homotopy = strategy_config.get(
            "physical_schedule_homotopy_endpoint"
        )
        if combined_expected:
            try:
                validate_physical_schedule_homotopy_endpoint(
                    raw_homotopy,
                    expected_binding_sha256=str(
                        validated_binding.get("binding_sha256", "")
                    ),
                    expected_first_layer_height_m=float(
                        design.get("first_layer_height_m")
                    ),
                    expected_layer_count=int(design.get("layer_count")),
                )
            except (
                CoarseDirectionReplayError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise CoarseRepairAuditError(
                    f"physical-schedule homotopy strategy is stale: {error}"
                ) from error
            if strategy_config.get("fixed_direction_homotopy_only") is not True:
                if not continuation_expected:
                    raise CoarseRepairAuditError(
                        "physical-schedule homotopy strategy is not exclusive"
                    )
            if raw_endpoint != raw_homotopy:
                raise CoarseRepairAuditError(
                    "physical-schedule homotopy endpoint aliases differ"
                )
            if homotopy_expected and (
                "first_frontier_direction_continuation_only"
                in strategy_config
                or "schedule_direction_continuation_endpoint"
                in strategy_config
            ):
                raise CoarseRepairAuditError(
                    "physical-schedule homotopy contains continuation state"
                )
            if continuation_expected and (
                strategy_config.get(
                    "first_frontier_direction_continuation_only"
                )
                is not True
                or "fixed_direction_homotopy_only" in strategy_config
                or not isinstance(
                    strategy_config.get(
                        "schedule_direction_continuation_endpoint"
                    ),
                    Mapping,
                )
            ):
                raise CoarseRepairAuditError(
                    "schedule direction continuation strategy is not exclusive"
                )
            if continuation_expected:
                evidence = expected_homotopy_audit_evidence
                quality = strategy_config.get("quality_improvement")
                if not isinstance(evidence, Mapping) or not isinstance(
                    quality, Mapping
                ):
                    raise CoarseRepairAuditError(
                        "schedule direction continuation lineage is incomplete"
                    )
                endpoint_arguments = {
                    "homotopy_manifest_sha256": str(
                        evidence.get("sha256", "")
                    ).casefold(),
                    "homotopy_discovery": evidence.get("discovery"),
                    "homotopy_endpoint_sha256": str(
                        raw_homotopy.get("endpoint_sha256", "")
                    ),
                    "direction_replay_approval_sha256": str(
                        expected_direction_replay_approval.get(
                            "approval_sha256", ""
                        )
                    ),
                    "local_schedule_binding_sha256": str(
                        validated_binding.get("binding_sha256", "")
                    ),
                    "minimum_prism_scaled_jacobian_for_pass": float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    "minimum_core_tetra_gamma_for_pass": float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                    "layer_count": int(design["layer_count"]),
                    "first_layer_height_m": float(
                        design["first_layer_height_m"]
                    ),
                }
                try:
                    validated_continuation = (
                        validate_schedule_frontier_direction_endpoint(
                            strategy_config[
                                "schedule_direction_continuation_endpoint"
                            ],
                            **endpoint_arguments,
                        )
                    )
                except (
                    CoarseScheduleContinuationError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise CoarseRepairAuditError(
                        "schedule direction continuation endpoint is stale: "
                        f"{error}"
                    ) from error
                if (
                    validated_continuation
                    != strategy_config[
                        "schedule_direction_continuation_endpoint"
                    ]
                ):
                    raise CoarseRepairAuditError(
                        "schedule direction continuation endpoint changed during validation"
                    )
        elif (
            "physical_schedule_homotopy_endpoint" in strategy_config
            or "fixed_direction_homotopy_only" in strategy_config
            or "first_frontier_direction_continuation_only" in strategy_config
            or "schedule_direction_continuation_endpoint" in strategy_config
        ):
            raise CoarseRepairAuditError(
                "ordinary physical replay contains a homotopy endpoint"
            )
    elif expected_schedule_feasibility_endpoint is None:
        if (
            "schedule_feasibility_endpoint" in strategy_config
            or "audit_purpose" in strategy_config
            or "combined_endpoint_only" in strategy_config
            or "owner_free_direction_endpoint" in strategy_config
            or "owner_free_direction_replay_approval" in strategy_config
            or "fixed_direction_replay_only" in strategy_config
            or "physical_schedule_homotopy_endpoint" in strategy_config
            or "fixed_direction_homotopy_only" in strategy_config
            or "first_frontier_direction_continuation_only" in strategy_config
            or "schedule_direction_continuation_endpoint" in strategy_config
        ):
            raise CoarseRepairAuditError(
                "ordinary repair audit unexpectedly contains a schedule endpoint"
            )
    else:
        if expected_schedule_feasibility_endpoint not in {
            "minimum-growth-existing-direction-repair",
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
        } or strategy_config.get("combined_endpoint_only") is not True:
            raise CoarseRepairAuditError(
                "combined schedule/direction endpoint scope is incomplete"
            )
        try:
            validate_minimum_growth_endpoint(
                raw_endpoint,
                expected_binding_sha256=str(
                    validated_binding.get("binding_sha256", "")
                ),
                expected_first_layer_height_m=float(
                    design.get("first_layer_height_m")
                ),
                expected_layer_count=int(design.get("layer_count")),
            )
        except (CoarseScheduleFeasibilityError, TypeError, ValueError) as error:
            raise CoarseRepairAuditError(
                f"combined schedule/direction endpoint is invalid: {error}"
            ) from error
        direction_endpoint = strategy_config.get(
            "owner_free_direction_endpoint"
        )
        if (
            expected_schedule_feasibility_endpoint
            == "minimum-growth-existing-direction-repair"
        ):
            if (
                strategy_config.get("audit_purpose")
                != "minimum_growth_plus_existing_direction_repair"
                or "owner_free_direction_endpoint" in strategy_config
            ):
                raise CoarseRepairAuditError(
                    "existing-direction endpoint scope is incomplete"
                )
        else:
            expected_direction_purpose = (
                "minimum_growth_owner_free_component_direction_audit"
                if expected_schedule_feasibility_endpoint
                == "minimum-growth-owner-free-component-direction-audit"
                else "minimum_growth_owner_free_component_pattern_audit"
            )
            if (
                strategy_config.get("audit_purpose")
                != expected_direction_purpose
            ):
                raise CoarseRepairAuditError(
                    "owner-free direction endpoint purpose is incomplete"
                )
            try:
                validated_direction_endpoint = validate_owner_free_direction_endpoint(
                    direction_endpoint,
                    expected_schedule_endpoint_sha256=str(
                        raw_endpoint.get("endpoint_sha256", "")
                        if isinstance(raw_endpoint, Mapping)
                        else ""
                    ),
                )
            except CoarseDirectionFeasibilityError as error:
                raise CoarseRepairAuditError(
                    f"owner-free direction endpoint is invalid: {error}"
                ) from error
            expected_direction_schema = (
                OWNER_FREE_DIRECTION_ENDPOINT_SCHEMA
                if expected_schedule_feasibility_endpoint
                == "minimum-growth-owner-free-component-direction-audit"
                else OWNER_FREE_PATTERN_ENDPOINT_SCHEMA
            )
            if (
                validated_direction_endpoint.get("schema")
                != expected_direction_schema
            ):
                raise CoarseRepairAuditError(
                    "owner-free direction endpoint schema does not match the requested audit"
                )
    return strategy_config


def _sha_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise CoarseRepairAuditError(f"{label} must be a list")
    normalized = [str(item).casefold() for item in value]
    if (
        len(normalized) != len(set(normalized))
        or any(_SHA256.fullmatch(item) is None for item in normalized)
        or normalized != sorted(normalized)
    ):
        raise CoarseRepairAuditError(f"{label} is not a sorted unique SHA-256 list")
    return normalized


def _physical_surface_schedule_values(
    binding: Mapping[str, Any],
    *,
    expected_surface_count: int,
    expected_layer_count: int,
) -> dict[str, list[float]]:
    """Project validated binding records to the validator's numeric schedules."""

    raw_schedules = binding.get("surface_schedules")
    if not isinstance(raw_schedules, Mapping):
        raise CoarseRepairAuditError(
            "physical replay surface schedules are missing"
        )
    schedules: dict[str, list[float]] = {}
    for raw_fingerprint, raw_record in raw_schedules.items():
        fingerprint = str(raw_fingerprint).casefold()
        if (
            _SHA256.fullmatch(fingerprint) is None
            or fingerprint in schedules
            or not isinstance(raw_record, Mapping)
            or str(raw_record.get("surface_fingerprint_id", "")).casefold()
            != fingerprint
        ):
            raise CoarseRepairAuditError(
                "physical replay surface schedule identity is invalid"
            )
        raw_values = raw_record.get("cumulative_heights_m")
        if not isinstance(raw_values, list) or len(raw_values) != int(
            expected_layer_count
        ):
            raise CoarseRepairAuditError(
                "physical replay cumulative surface schedule is incomplete"
            )
        values: list[float] = []
        for raw_value in raw_values:
            if isinstance(raw_value, bool):
                raise CoarseRepairAuditError(
                    "physical replay cumulative height is not finite"
                )
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as error:
                raise CoarseRepairAuditError(
                    "physical replay cumulative height is not finite"
                ) from error
            if not math.isfinite(value) or value <= 0.0:
                raise CoarseRepairAuditError(
                    "physical replay cumulative height is not finite"
                )
            values.append(value)
        if any(right <= left for left, right in zip(values, values[1:])):
            raise CoarseRepairAuditError(
                "physical replay cumulative surface schedule is not monotone"
            )
        schedules[fingerprint] = values
    if len(schedules) != int(expected_surface_count):
        raise CoarseRepairAuditError(
            "physical replay surface schedule coverage is incomplete"
        )
    return dict(sorted(schedules.items()))


def _validate_stable_signature(discovery: Mapping[str, Any]) -> dict[str, Any]:
    signature = discovery.get("stable_signature")
    if not isinstance(signature, Mapping) or set(signature) != {
        "observed_counts",
        "wall_surface_fingerprint_inventory",
        "initial_bad_one_layer_prisms",
        "reversed_one_layer_prisms",
        "repair_sites",
    }:
        raise CoarseRepairAuditError("repair stable signature schema is incomplete")
    configured_hash = str(discovery.get("stable_signature_sha256", "")).casefold()
    if (
        _SHA256.fullmatch(configured_hash) is None
        or _canonical_sha256(signature) != configured_hash
    ):
        raise CoarseRepairAuditError("repair stable signature hash is stale")

    counts = signature.get("observed_counts")
    if (
        not isinstance(counts, Mapping)
        or set(counts) != _OBSERVED_COUNT_FIELDS
        or counts != discovery.get("observed_counts")
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        )
        or int(counts.get("projected_3d_element_count", 0)) <= 0
    ):
        raise CoarseRepairAuditError("repair observed counts are incomplete")
    wall_inventory = _sha_list(
        signature.get("wall_surface_fingerprint_inventory"),
        "wall fingerprint inventory",
        allow_empty=False,
    )
    if len(wall_inventory) != 48:
        raise CoarseRepairAuditError("repair audit must preserve all 48 wall fingerprints")

    prism_fields = {"source_triangle_sha256", "wall_surface_fingerprint"}
    for name, count_key in (
        ("initial_bad_one_layer_prisms", "initial_bad_prism_count"),
        ("reversed_one_layer_prisms", "negative_volume_prism_count"),
    ):
        records = signature.get(name)
        if not isinstance(records, list) or len(records) != counts[count_key]:
            raise CoarseRepairAuditError(f"{name} count differs from observed counts")
        for record in records:
            if (
                not isinstance(record, Mapping)
                or set(record) != prism_fields
                or _SHA256.fullmatch(
                    str(record.get("source_triangle_sha256", "")).casefold()
                )
                is None
                or str(record.get("wall_surface_fingerprint", "")).casefold()
                not in wall_inventory
            ):
                raise CoarseRepairAuditError(f"{name} has an invalid stable record")
        stable_pairs = [
            (
                str(record["wall_surface_fingerprint"]),
                str(record["source_triangle_sha256"]),
            )
            for record in records
        ]
        if stable_pairs != sorted(stable_pairs) or len(stable_pairs) != len(
            set(stable_pairs)
        ):
            raise CoarseRepairAuditError(f"{name} is not sorted and unique")

    site_fields = {
        "source_triangle_sha256",
        "wall_surface_fingerprint",
        "assignment_method",
        "owner_root_coordinate_sha256",
        "candidate_owner_root_coordinate_sha256",
        "neighbour_root_coordinate_sha256",
        "protected_root_coordinate_sha256",
    }
    sites = signature.get("repair_sites")
    if (
        not isinstance(sites, list)
        or len(sites) != counts["bad_source_triangle_count"]
    ):
        raise CoarseRepairAuditError("repair-site count differs from observed counts")
    seen_sites: set[tuple[str, str]] = set()
    for site in sites:
        if not isinstance(site, Mapping) or set(site) != site_fields:
            raise CoarseRepairAuditError("repair stable site schema is incomplete")
        triangle = str(site.get("source_triangle_sha256", "")).casefold()
        wall = str(site.get("wall_surface_fingerprint", "")).casefold()
        owner = site.get("owner_root_coordinate_sha256")
        if (
            _SHA256.fullmatch(triangle) is None
            or wall not in wall_inventory
            or (owner is not None and _SHA256.fullmatch(str(owner).casefold()) is None)
            or not str(site.get("assignment_method", ""))
            or (triangle, wall) in seen_sites
        ):
            raise CoarseRepairAuditError("repair stable site identity is invalid")
        seen_sites.add((triangle, wall))
        method = str(site["assignment_method"])
        if discovery.get("status") == "PASS" and (
            owner is None
            or site.get("candidate_owner_root_coordinate_sha256") != []
            or method == "AUDIT_ONLY_UNRESOLVED"
        ):
            raise CoarseRepairAuditError(
                "complete repair site lacks one assigned stable owner"
            )
        for field in (
            "candidate_owner_root_coordinate_sha256",
            "neighbour_root_coordinate_sha256",
            "protected_root_coordinate_sha256",
        ):
            _sha_list(site.get(field), f"repair site {field}")
    site_order = [
        (
            str(site["wall_surface_fingerprint"]),
            str(site["source_triangle_sha256"]),
        )
        for site in sites
    ]
    if site_order != sorted(site_order):
        raise CoarseRepairAuditError("repair stable sites are not sorted")

    # A future accidental runtime-tag insertion must invalidate the stable
    # schema instead of silently changing what the signature means.
    def reject_runtime_keys(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                lowered = str(key).casefold()
                if lowered.endswith("_tag") or lowered.endswith("_tags") or (
                    "audit" in lowered
                ):
                    raise CoarseRepairAuditError(
                        "runtime/audit tags are forbidden in the stable signature"
                    )
                reject_runtime_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_runtime_keys(nested)

    reject_runtime_keys(signature)
    return copy.deepcopy(dict(signature))


def validate_coarse_repair_discovery(value: Any) -> dict[str, Any]:
    """Return a defensive copy of one strictly validated discovery record.

    This is the single read-only schema gate shared by orchestration callers;
    it deliberately validates observed counts, stable repair-site identities,
    ambiguity evidence and the absence of runtime tags from the stable hash.
    """

    if not isinstance(value, Mapping):
        raise CoarseRepairAuditError("repair strategy returned no discovery object")
    status = value.get("status")
    complete = value.get("profile_complete")
    if (
        value.get("schema") != DISCOVERY_SCHEMA
        or status not in {"PASS", "INCOMPLETE"}
        or complete is not (status == "PASS")
        or value.get("audit_only") is not True
        or value.get("calibration_PASS_authorized") is not False
        or value.get("production_mesh_eligible") is not False
        or value.get("mesh_written") is not False
        or value.get("runtime_mesh_tags_hardcoded") is not False
        or value.get("runtime_tags_are_audit_only") is not True
        or value.get("repair_application_performed") is not True
        or value.get("in_memory_mesh_mutation_performed") is not True
    ):
        raise CoarseRepairAuditError("repair discovery violates audit-only scope")
    _validate_stable_signature(value)
    if status == "INCOMPLETE":
        ambiguities = value.get("owner_ambiguities")
        if (
            value.get("repair_application_scope")
            != "IN_MEMORY_PRE_SMOOTHING_AUDIT_ONLY"
            or value.get("final_smoothing_application_performed") is not False
            or not isinstance(ambiguities, list)
            or not ambiguities
            or value.get("incomplete_reason")
            != "AMBIGUOUS_REPAIR_OWNER_REQUIRES_EXTERNAL_APPROVAL"
        ):
            raise CoarseRepairAuditError(
                "incomplete repair discovery lacks structured ambiguity evidence"
            )
        sites = value["stable_signature"]["repair_sites"]
        unresolved_sites = {
            (
                str(site["source_triangle_sha256"]),
                str(site["wall_surface_fingerprint"]),
            ): site
            for site in sites
            if site["assignment_method"] == "AUDIT_ONLY_UNRESOLVED"
        }
        ambiguity_sites: dict[tuple[str, str], set[str]] = {}
        for ambiguity in ambiguities:
            if not isinstance(ambiguity, Mapping):
                raise CoarseRepairAuditError("one owner ambiguity is not a mapping")
            key = (
                str(ambiguity.get("source_triangle_sha256", "")).casefold(),
                str(ambiguity.get("wall_surface_fingerprint", "")).casefold(),
            )
            reason = str(ambiguity.get("reason", ""))
            candidate_values = _sha_list(
                ambiguity.get("candidate_root_coordinate_sha256"),
                "owner ambiguity candidates",
            )
            if (
                reason
                == "MULTIPLE_SHARP_ROOTS_WITHOUT_UNIQUE_STABLE_OWNER"
                and len(candidate_values) < 2
            ) or (
                reason == "NO_UNIQUE_ONE_RING_SHARP_ROOT_GROUP"
                and len(candidate_values) == 1
            ) or reason not in {
                "MULTIPLE_SHARP_ROOTS_WITHOUT_UNIQUE_STABLE_OWNER",
                "NO_UNIQUE_ONE_RING_SHARP_ROOT_GROUP",
                "STRICT_FAILURE_WITHOUT_UNIQUE_DIRECT_SHARP_ROOT",
            }:
                raise CoarseRepairAuditError(
                    "owner ambiguity reason/candidate cardinality is inconsistent"
                )
            if (
                reason == "STRICT_FAILURE_WITHOUT_UNIQUE_DIRECT_SHARP_ROOT"
                and len(candidate_values) == 1
            ):
                raise CoarseRepairAuditError(
                    "strict-failure ambiguity unexpectedly has one direct owner"
                )
            candidates = set(candidate_values)
            if key in ambiguity_sites:
                raise CoarseRepairAuditError("owner ambiguity identity is duplicated")
            ambiguity_sites[key] = candidates
        if set(ambiguity_sites) != set(unresolved_sites) or any(
            ambiguity_sites[key]
            != set(unresolved_sites[key]["candidate_owner_root_coordinate_sha256"])
            for key in ambiguity_sites
        ):
            raise CoarseRepairAuditError(
                "owner ambiguity candidates differ from stable repair sites"
            )
        if value["observed_counts"]["owner_ambiguity_count"] != len(
            ambiguity_sites
        ):
            raise CoarseRepairAuditError(
                "owner ambiguity count differs from stable repair sites"
            )
    elif (
        value.get("repair_application_scope")
        != "IN_MEMORY_COMPLETE_AUDIT_ONLY"
        or value.get("final_smoothing_application_performed") is not True
        or value.get("owner_ambiguities") != []
    ):
        raise CoarseRepairAuditError("complete repair discovery is internally inconsistent")
    return copy.deepcopy(dict(value))


def _validate_discovery(value: Any) -> dict[str, Any]:
    """Backward-compatible private alias for the public strict schema gate."""

    return validate_coarse_repair_discovery(value)


def run_coarse_repair_audit(
    *,
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    strategy: RealProjectBoundaryLayerStrategy,
    local_schedule_binding: Mapping[str, Any],
    projection_evidence: Mapping[str, Any],
    gmsh_module: Any | None = None,
    run_evidence: Mapping[str, Any] | None = None,
    schedule_feasibility_endpoint: str | None = None,
    direction_replay_approval: Mapping[str, Any] | None = None,
    homotopy_audit_evidence: Mapping[str, Any] | None = None,
    strategy_config_factory: Callable[..., dict[str, Any]] = (
        make_coarse_repair_audit_strategy_config
    ),
    projection_validator: Callable[..., dict[str, Any]] = (
        validate_projection_evidence_record
    ),
) -> dict[str, Any]:
    """Run one fresh Gmsh discovery session without writing a mesh."""

    started = _utc_now()
    monotonic_started = time.monotonic()
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "status": "FAIL",
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "started_at_utc": started,
        "ended_at_utc": None,
        "elapsed_seconds": None,
        "coarse_contract_sha256": contract.get("normalized_config_sha256"),
        "characteristic_length_m": characteristic_length_m,
        "audit_purpose": (
            (
                SCHEDULE_CONTINUATION_PURPOSE
                if schedule_feasibility_endpoint
                == SCHEDULE_CONTINUATION_REQUEST
                else (
                    PHYSICAL_HOMOTOPY_PURPOSE
                    if schedule_feasibility_endpoint
                    == PHYSICAL_HOMOTOPY_REQUEST
                    else "physical_local_schedule_fixed_direction_replay"
                )
            )
            if direction_replay_approval is not None
            else (
                "repair_profile_discovery"
                if schedule_feasibility_endpoint is None
                else (
                    "minimum_growth_plus_existing_direction_repair"
                    if schedule_feasibility_endpoint
                    == "minimum-growth-existing-direction-repair"
                    else (
                        "minimum_growth_owner_free_component_direction_audit"
                        if schedule_feasibility_endpoint
                        == "minimum-growth-owner-free-component-direction-audit"
                        else "minimum_growth_owner_free_component_pattern_audit"
                    )
                )
            )
        ),
        "schedule_feasibility_endpoint_requested": schedule_feasibility_endpoint,
        "direction_replay_approval": copy.deepcopy(
            dict(direction_replay_approval)
        )
        if isinstance(direction_replay_approval, Mapping)
        else None,
        "physical_schedule_homotopy_endpoint": None,
        "schedule_direction_continuation_endpoint": None,
        "homotopy_audit_source": (
            {
                key: homotopy_audit_evidence.get(key)
                for key in ("path", "sha256", "size_bytes", "manifest_status")
            }
            if isinstance(homotopy_audit_evidence, Mapping)
            else None
        ),
        "projection_evidence": None,
        "local_schedule_binding": copy.deepcopy(dict(local_schedule_binding)),
        "strategy_config": None,
        "strategy_config_sha256": None,
        "run_evidence": copy.deepcopy(dict(run_evidence or {})),
        "source_before": None,
        "source_after": None,
        "source_unchanged": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_session": {
            "initialize_attempted": False,
            "initialize_called": False,
            "post_initialize_state_check_attempted": False,
            "post_initialize_state": None,
            "post_initialize_state_check_error": None,
            "logger_start_attempted": False,
            "logger_started": False,
            "logger_get_attempted": False,
            "logger_capture_succeeded": False,
            "logger_messages": [],
            "logger_stop_attempted": False,
            "logger_stopped": False,
            "diagnostic_audit": None,
            "write_guard": {
                "installed": False,
                "write_attempt_count": 0,
                "blocked_paths": [],
            },
            "clear_called": False,
            "finalize_attempted": False,
            "finalize_called": False,
            "cleanup_errors": [],
        },
        "repair_discovery": None,
        "strategy_evidence": {},
        "error": None,
    }
    source: Path | None = None
    gmsh: Any | None = None
    gmsh_guard: _AuditOnlyGmshProxy | None = None
    logger_started = False
    try:
        characteristic = float(characteristic_length_m)
        if not math.isfinite(characteristic) or characteristic <= 0.0:
            raise CoarseRepairAuditError(
                "repair audit characteristic length must be finite and positive"
            )
        if (
            schedule_feasibility_endpoint is not None
            and direction_replay_approval is not None
            and schedule_feasibility_endpoint
            not in (PHYSICAL_HOMOTOPY_REQUEST, SCHEDULE_CONTINUATION_REQUEST)
        ):
            raise CoarseRepairAuditError(
                "repair audit search and fixed replay are mutually exclusive"
            )
        configured_lengths = contract.get("calibration_characteristic_lengths_m")
        if not isinstance(configured_lengths, list) or not configured_lengths:
            raise CoarseRepairAuditError("coarse contract has no calibration points")
        first_characteristic = float(configured_lengths[0])
        if not math.isclose(
            characteristic, first_characteristic, rel_tol=1.0e-12, abs_tol=0.0
        ):
            raise CoarseRepairAuditError(
                "repair discovery is restricted to the first projected point"
            )
        try:
            validated_schedule_binding = validate_boundary_layer_schedule_binding(
                contract, local_schedule_binding
            )
        except BoundaryLayerScheduleBindingError as error:
            raise CoarseRepairAuditError(
                f"repair audit local schedule binding is invalid: {error}"
            ) from error
        manifest["local_schedule_binding"] = copy.deepcopy(
            validated_schedule_binding
        )
        projected_count = projection_evidence.get("projected_3d_elements")
        if (
            isinstance(projected_count, bool)
            or not isinstance(projected_count, int)
            or projected_count <= 0
        ):
            raise CoarseRepairAuditError("projection evidence has no positive count")
        try:
            validated_projection = projection_validator(
                projection_evidence,
                expected_path=str(projection_evidence.get("path", "")),
                expected_sha256=str(projection_evidence.get("sha256", "")),
                expected_config_sha256=str(
                    projection_evidence.get("config_sha256", "")
                ),
                expected_coarse_contract_sha256=str(
                    contract.get("normalized_config_sha256", "")
                ),
                expected_characteristic_length_m=first_characteristic,
                expected_projected_3d_elements=projected_count,
            )
        except CoarseProjectionEvidenceError as error:
            raise CoarseRepairAuditError(
                f"repair audit projection evidence is invalid: {error}"
            ) from error
        manifest["projection_evidence"] = validated_projection

        strategy_factory_arguments: dict[str, Any] = {
            "local_schedule_binding": validated_schedule_binding,
        }
        if schedule_feasibility_endpoint is not None:
            strategy_factory_arguments["schedule_feasibility_endpoint"] = (
                schedule_feasibility_endpoint
            )
        if direction_replay_approval is not None:
            strategy_factory_arguments["direction_replay_approval"] = (
                direction_replay_approval
            )
        if homotopy_audit_evidence is not None:
            strategy_factory_arguments["homotopy_audit_evidence"] = (
                homotopy_audit_evidence
            )
        strategy_config = strategy_config_factory(
            contract, characteristic, **strategy_factory_arguments
        )
        strategy_config = _validate_repair_audit_strategy_config(
            contract=contract,
            characteristic_length_m=characteristic,
            strategy_config=strategy_config,
            validated_binding=validated_schedule_binding,
            expected_schedule_feasibility_endpoint=(
                schedule_feasibility_endpoint
            ),
            expected_direction_replay_approval=direction_replay_approval,
            expected_homotopy_audit_evidence=homotopy_audit_evidence,
        )
        manifest["strategy_config"] = strategy_config
        manifest["strategy_config_sha256"] = strategy_config.get(
            "normalized_config_sha256"
        )
        manifest["physical_schedule_homotopy_endpoint"] = copy.deepcopy(
            strategy_config.get("physical_schedule_homotopy_endpoint")
        )
        manifest["schedule_direction_continuation_endpoint"] = copy.deepcopy(
            strategy_config.get("schedule_direction_continuation_endpoint")
        )

        source = Path(str(contract.get("pipeline_brep_path", ""))).expanduser().resolve(
            strict=True
        )
        source_before = _snapshot(source)
        if (
            not source.is_file()
            or source.suffix.casefold() != ".brep"
            or source_before["read_only"] is not True
            or source_before["sha256"]
            != contract.get("provenance", {}).get("pipeline_brep_sha256")
        ):
            raise CoarseRepairAuditError(
                "repair audit source is not the hash-bound read-only BREP"
            )
        manifest["source_before"] = source_before

        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_path = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_path).resolve()) if module_path else "unknown"
        )
        checker = getattr(gmsh, "isInitialized", None)
        if callable(checker) and bool(checker()):
            raise CoarseRepairAuditError(
                "repair discovery requires a fresh uninitialized Gmsh session"
            )
        manifest["gmsh_session"]["initialize_attempted"] = True
        gmsh.initialize(readConfigFiles=False)
        manifest["gmsh_session"]["initialize_called"] = True
        logger = getattr(gmsh, "logger", None)
        starter = getattr(logger, "start", None)
        if logger is None or not callable(starter):
            raise CoarseRepairAuditError(
                "repair discovery requires a startable Gmsh logger"
            )
        manifest["gmsh_session"]["logger_start_attempted"] = True
        starter()
        logger_started = True
        manifest["gmsh_session"]["logger_started"] = True
        gmsh_guard = _AuditOnlyGmshProxy(gmsh)
        manifest["gmsh_session"]["write_guard"]["installed"] = True
        gmsh.model.add("cfdpipe_coarse_repair_audit")
        raw_discovery = strategy.build(
            gmsh_guard, source, strategy_config, source.parent
        )
        if direction_replay_approval is not None:
            try:
                source_quality = direction_replay_approval[
                    "source_minimum_growth_final_quality"
                ]
                expected_layer_count = int(
                    contract["boundary_layer_design"]["layer_count"]
                )
                expected_surface_count = len(
                    contract["wall_surface_fingerprints"]
                )
                expected_surface_schedules = (
                    _physical_surface_schedule_values(
                        validated_schedule_binding,
                        expected_surface_count=expected_surface_count,
                        expected_layer_count=expected_layer_count,
                    )
                )
                continuation_requested = (
                    schedule_feasibility_endpoint
                    == SCHEDULE_CONTINUATION_REQUEST
                )
                validator = (
                    validate_schedule_frontier_direction_continuation
                    if continuation_requested
                    else (
                        validate_physical_schedule_homotopy_discovery
                        if schedule_feasibility_endpoint
                        == PHYSICAL_HOMOTOPY_REQUEST
                        else validate_physical_schedule_replay_discovery
                    )
                )
                validator_arguments: dict[str, Any] = {
                    "expected_approval": direction_replay_approval,
                    "expected_projected_3d_elements": int(
                        validated_projection["projected_3d_elements"]
                    ),
                    "expected_prism_element_count": int(
                        source_quality["prism_element_count"]
                    ),
                    "expected_core_element_count": int(
                        source_quality["core_element_count"]
                    ),
                    "expected_layer_count": expected_layer_count,
                    "expected_surface_count": expected_surface_count,
                    "expected_surface_schedules": expected_surface_schedules,
                    "expected_minimum_prism_scaled_jacobian": float(
                        strategy_config["quality_improvement"][
                            "minimum_prism_scaled_jacobian"
                        ]
                    ),
                    "expected_minimum_core_tetra_gamma": float(
                        strategy_config["quality_improvement"][
                            "minimum_core_tetra_gamma"
                        ]
                    ),
                }
                if continuation_requested:
                    validator_arguments = {
                        "expected_endpoint": strategy_config[
                            "schedule_direction_continuation_endpoint"
                        ],
                        "expected_projected_3d_elements": int(
                            validated_projection["projected_3d_elements"]
                        ),
                        "expected_prism_element_count": int(
                            source_quality["prism_element_count"]
                        ),
                        "expected_core_element_count": int(
                            source_quality["core_element_count"]
                        ),
                    }
                elif (
                    schedule_feasibility_endpoint
                    == PHYSICAL_HOMOTOPY_REQUEST
                ):
                    validator_arguments["expected_endpoint"] = strategy_config[
                        "physical_schedule_homotopy_endpoint"
                    ]
                discovery = validator(raw_discovery, **validator_arguments)
            except (
                CoarseDirectionReplayError,
                CoarseScheduleContinuationError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise CoarseRepairAuditError(
                    f"physical-schedule replay discovery is invalid: {error}"
                ) from error
            expected_physical_schema = (
                SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA
                if schedule_feasibility_endpoint
                == SCHEDULE_CONTINUATION_REQUEST
                else (
                    HOMOTOPY_DISCOVERY_SCHEMA
                    if schedule_feasibility_endpoint
                    == PHYSICAL_HOMOTOPY_REQUEST
                    else PHYSICAL_DISCOVERY_SCHEMA
                )
            )
            if discovery.get("schema") != expected_physical_schema:
                raise CoarseRepairAuditError(
                    "physical-schedule replay discovery schema differs"
                )
        elif (
            schedule_feasibility_endpoint
            in {
                "minimum-growth-owner-free-component-direction-audit",
                "minimum-growth-owner-free-component-pattern-audit",
            }
        ):
            try:
                discovery = validate_component_direction_discovery(
                    raw_discovery,
                    expected_direction_endpoint=strategy_config[
                        "owner_free_direction_endpoint"
                    ],
                )
            except (CoarseComponentDirectionError, KeyError) as error:
                raise CoarseRepairAuditError(
                    f"owner-free component direction discovery is invalid: {error}"
                ) from error
            if (
                discovery.get("schema") != COMPONENT_DIRECTION_DISCOVERY_SCHEMA
                or discovery["direction_endpoint"]
                != strategy_config["owner_free_direction_endpoint"]
                or discovery["observed_counts"][
                    "projected_3d_element_count"
                ]
                != int(validated_projection["projected_3d_elements"])
                or discovery["stable_observation"][
                    "wall_surface_fingerprint_inventory"
                ]
                != sorted(
                    str(value).casefold()
                    for value in contract["wall_surface_fingerprints"]
                )
                or float(
                    discovery["final_quality"][
                        "minimum_prism_scaled_jacobian_for_pass"
                    ]
                )
                != float(
                    strategy_config["quality_improvement"][
                        "minimum_prism_scaled_jacobian"
                    ]
                )
                or float(
                    discovery["final_quality"][
                        "minimum_core_tetra_gamma_for_pass"
                    ]
                )
                != float(
                    strategy_config["quality_improvement"][
                        "minimum_core_tetra_gamma"
                    ]
                )
                or (
                    schedule_feasibility_endpoint
                    == "minimum-growth-owner-free-component-pattern-audit"
                    and float(
                        discovery["search"]["tangent_pattern_refinement"][
                            "minimum_required_changed_direction_margin"
                        ]
                    )
                    != float(
                        strategy_config["post_mesh_repair"][
                            "minimum_smoothing_margin"
                        ]
                    )
                )
                or (
                    schedule_feasibility_endpoint
                    == "minimum-growth-owner-free-component-pattern-audit"
                    and float(
                        discovery["search"]["tangent_pattern_refinement"][
                            "minimum_required_cone_margin"
                        ]
                    )
                    != float(
                        strategy_config["post_mesh_repair"][
                            "minimum_cone_margin"
                        ]
                    )
                )
            ):
                raise CoarseRepairAuditError(
                    "owner-free direction discovery lineage differs from the trusted run"
                )
        else:
            discovery = validate_coarse_repair_discovery(raw_discovery)
        manifest["repair_discovery"] = discovery
        evidence_method = getattr(strategy, "evidence", None)
        if callable(evidence_method):
            raw_evidence = evidence_method("build")
            if not isinstance(raw_evidence, Mapping):
                raise CoarseRepairAuditError("repair strategy evidence is not a mapping")
            manifest["strategy_evidence"] = copy.deepcopy(dict(raw_evidence))
        manifest["status"] = str(discovery["status"])
    except BaseException as error:
        manifest["status"] = "FAIL"
        manifest["error"] = _error_record(error)
    finally:
        if gmsh_guard is not None:
            manifest["gmsh_session"]["write_guard"]["write_attempt_count"] = len(
                gmsh_guard.write_attempts
            )
            manifest["gmsh_session"]["write_guard"]["blocked_paths"] = list(
                gmsh_guard.write_attempts
            )
        if gmsh is not None and logger_started:
            logger = getattr(gmsh, "logger", None)
            try:
                getter = getattr(logger, "get", None)
                if not callable(getter):
                    raise CoarseRepairAuditError(
                        "started Gmsh logger has no callable get method"
                    )
                manifest["gmsh_session"]["logger_get_attempted"] = True
                manifest["gmsh_session"]["logger_messages"] = [
                    str(value) for value in getter()
                ]
                manifest["gmsh_session"]["logger_capture_succeeded"] = True
            except BaseException as error:
                manifest["gmsh_session"]["cleanup_errors"].append(
                    {"stage": "gmsh.logger.get", **_error_record(error)}
                )
            try:
                stopper = getattr(logger, "stop", None)
                if not callable(stopper):
                    raise CoarseRepairAuditError(
                        "started Gmsh logger has no callable stop method"
                    )
                manifest["gmsh_session"]["logger_stop_attempted"] = True
                stopper()
                manifest["gmsh_session"]["logger_stopped"] = True
            except BaseException as error:
                manifest["gmsh_session"]["cleanup_errors"].append(
                    {"stage": "gmsh.logger.stop", **_error_record(error)}
                )
        logger_diagnostic_failed = False
        if (
            manifest["gmsh_session"]["logger_started"] is True
            and manifest["gmsh_session"]["logger_capture_succeeded"] is True
        ):
            diagnostic_manifest = {
                "gmsh_sessions": {"audit": manifest["gmsh_session"]}
            }
            try:
                _reject_fatal_gmsh_log(diagnostic_manifest, "audit")
            except BaseException as error:
                logger_diagnostic_failed = True
                if manifest["error"] is None:
                    manifest["error"] = _error_record(
                        CoarseRepairAuditError(str(error))
                    )
        else:
            logger_diagnostic_failed = True
            manifest["gmsh_session"]["diagnostic_audit"] = {
                "status": "FAIL",
                "raw_messages_preserved": True,
                "fatal_messages": [],
                "pending_diagnostics": [],
                "classified_nonfatal_diagnostics": [],
                "associated_summary_lines": [],
                "failure_reason": "GMSH_LOGGER_NOT_STARTED_OR_CAPTURED",
                "policy": "audit logger startup and complete capture are mandatory",
            }
        if (
            gmsh is not None
            and manifest["gmsh_session"]["initialize_attempted"]
            and manifest["gmsh_session"]["initialize_called"] is not True
        ):
            checker = getattr(gmsh, "isInitialized", None)
            if callable(checker):
                manifest["gmsh_session"][
                    "post_initialize_state_check_attempted"
                ] = True
                try:
                    manifest["gmsh_session"]["post_initialize_state"] = bool(
                        checker()
                    )
                except BaseException as error:
                    manifest["gmsh_session"][
                        "post_initialize_state_check_error"
                    ] = _error_record(error)
        if (
            gmsh is not None
            and manifest["gmsh_session"]["initialize_attempted"]
        ):
            try:
                gmsh.clear()
                manifest["gmsh_session"]["clear_called"] = True
            except BaseException as error:
                manifest["gmsh_session"]["cleanup_errors"].append(
                    {"stage": "gmsh.clear", **_error_record(error)}
                )
            manifest["gmsh_session"]["finalize_attempted"] = True
            try:
                gmsh.finalize()
                manifest["gmsh_session"]["finalize_called"] = True
            except BaseException as error:
                manifest["gmsh_session"]["cleanup_errors"].append(
                    {"stage": "gmsh.finalize", **_error_record(error)}
                )
        if source is not None and manifest["source_before"] is not None:
            try:
                manifest["source_after"] = _snapshot(source)
                manifest["source_unchanged"] = (
                    manifest["source_after"] == manifest["source_before"]
                )
            except BaseException as error:
                manifest["source_snapshot_error"] = _error_record(error)
        manifest["ended_at_utc"] = _utc_now()
        manifest["elapsed_seconds"] = time.monotonic() - monotonic_started
        cleanup_failed = bool(manifest["gmsh_session"]["cleanup_errors"])
        write_guard = manifest["gmsh_session"]["write_guard"]
        if (
            cleanup_failed
            or logger_diagnostic_failed
            or manifest.get("source_unchanged") is not True
            or manifest["gmsh_session"]["initialize_called"] is not True
            or manifest["gmsh_session"]["logger_started"] is not True
            or manifest["gmsh_session"]["logger_capture_succeeded"] is not True
            or manifest["gmsh_session"]["logger_stopped"] is not True
            or write_guard["installed"] is not True
            or write_guard["write_attempt_count"] != 0
            or manifest["gmsh_session"]["finalize_called"] is not True
        ):
            manifest["status"] = "FAIL"
            if manifest["error"] is None:
                manifest["error"] = {
                    "type": "CoarseRepairAuditError",
                    "message": (
                        "repair audit lifecycle/source-integrity gate failed"
                    ),
                    "traceback": "",
                }
    return manifest


__all__ = [
    "CoarseRepairAuditError",
    "DISCOVERY_SCHEMA",
    "MANIFEST_SCHEMA",
    "PHYSICAL_HOMOTOPY_PURPOSE",
    "PHYSICAL_HOMOTOPY_REQUEST",
    "RESOURCE_ABORT_MANIFEST_NAME",
    "RESOURCE_ABORT_MANIFEST_SCHEMA",
    "SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA",
    "SCHEDULE_CONTINUATION_PURPOSE",
    "SCHEDULE_CONTINUATION_REQUEST",
    "load_and_validate_physical_homotopy_audit_manifest",
    "make_coarse_repair_audit_strategy_config",
    "publish_coarse_repair_resource_abort_manifest",
    "run_coarse_repair_audit",
    "validate_coarse_repair_discovery",
    "validate_coarse_repair_resource_abort_manifest",
    "validate_schedule_frontier_direction_continuation",
]
