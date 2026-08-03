"""Strict, standard-library-only validation of coarse projection evidence.

The count-only projection is the authorization gate for the first (coarsest)
calibration point.  Both the controller and the isolated calibration worker
use this module so a short or stale projection manifest cannot be promoted to
a calibration input.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping


PROJECTION_MANIFEST_SCHEMA = "cfdpipe.coarse_mesh_projection_manifest.v1"
PROJECTION_EVIDENCE_SCHEMA = "cfdpipe.coarse_mesh_projection_evidence.v1"
PROJECTION_ELEMENT_CAP_EXCLUSIVE = 300_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseProjectionEvidenceError(RuntimeError):
    """Projection evidence is missing, stale, or does not prove its scope."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CoarseProjectionEvidenceError(
            "projection evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoarseProjectionEvidenceError(
                f"projection manifest contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CoarseProjectionEvidenceError(
        f"projection manifest contains non-finite constant {value}"
    )


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _read_manifest(
    path: str | os.PathLike[str],
    expected_sha256: str,
    *,
    output_root: str | os.PathLike[str],
) -> tuple[dict[str, Any], Path, str, int]:
    expected = str(expected_sha256)
    if not _is_sha256(expected):
        raise CoarseProjectionEvidenceError(
            "projection manifest SHA-256 must be 64 lowercase hexadecimal digits"
        )
    requested = Path(path).expanduser()
    if ".." in requested.parts:
        raise CoarseProjectionEvidenceError(
            "projection manifest path must not contain '..'"
        )
    lexical = Path(os.path.abspath(requested))
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and _is_link_like(candidate):
            raise CoarseProjectionEvidenceError(
                "projection manifest path must not use links or junctions"
            )
    try:
        resolved = lexical.resolve(strict=True)
        root = Path(output_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise CoarseProjectionEvidenceError(
            "projection manifest or coarse output root does not exist"
        ) from error
    if (
        not resolved.is_file()
        or resolved.name != "projection_manifest.json"
        or root not in resolved.parents
        or resolved.parent.parent != root
        or resolved.parent.name.casefold() == "_worker_evidence"
    ):
        raise CoarseProjectionEvidenceError(
            "projection manifest must be a direct run artifact below runs/mesh/coarse"
        )
    try:
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except OSError as error:
        raise CoarseProjectionEvidenceError(
            f"cannot read projection manifest: {error}"
        ) from error
    if (
        before.st_size <= 0
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != before.st_size
    ):
        raise CoarseProjectionEvidenceError(
            "projection manifest changed while it was being read"
        )
    actual = _sha256_bytes(payload)
    if actual != expected:
        raise CoarseProjectionEvidenceError("projection manifest SHA-256 is stale")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseProjectionEvidenceError(
            f"projection manifest is not strict UTF-8 JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise CoarseProjectionEvidenceError(
            "projection manifest root must be an object"
        )
    return document, resolved, actual, len(payload)


def _matching_path(value: object, expected: Path) -> bool:
    raw = str(value).replace("\\", "/")
    expected_raw = str(expected).replace("\\", "/")
    for anchor in ("/config/", "/runs/"):
        raw_index = raw.casefold().find(anchor)
        expected_index = expected_raw.casefold().find(anchor)
        if raw_index >= 0 and expected_index >= 0:
            return (
                raw[raw_index:].casefold()
                == expected_raw[expected_index:].casefold()
            )
    try:
        actual = Path(str(value)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def _positive_finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def load_and_validate_projection_evidence(
    manifest_path: str | os.PathLike[str],
    manifest_sha256: str,
    *,
    output_root: str | os.PathLike[str],
    expected_config_path: str | os.PathLike[str],
    expected_config_sha256: str,
    expected_coarse_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_process_memory_limit_bytes: int,
) -> dict[str, Any]:
    """Load one explicit projection manifest and return self-hashed evidence."""

    expected_config_hash = str(expected_config_sha256)
    expected_contract_hash = str(expected_coarse_contract_sha256)
    if not _is_sha256(expected_config_hash) or not _is_sha256(
        expected_contract_hash
    ):
        raise CoarseProjectionEvidenceError(
            "expected config/contract SHA-256 values are invalid"
        )
    if (
        isinstance(expected_process_memory_limit_bytes, bool)
        or not isinstance(expected_process_memory_limit_bytes, int)
        or expected_process_memory_limit_bytes <= 0
    ):
        raise CoarseProjectionEvidenceError(
            "expected projection worker memory limit is invalid"
        )
    if not _positive_finite(expected_characteristic_length_m):
        raise CoarseProjectionEvidenceError(
            "expected projection characteristic length is invalid"
        )
    document, resolved, actual_sha256, size_bytes = _read_manifest(
        manifest_path, manifest_sha256, output_root=output_root
    )
    expected_config = Path(expected_config_path).expanduser().resolve(strict=True)
    output_directory = resolved.parent.resolve(strict=True)
    session = document.get("gmsh_session")
    worker = document.get("worker")
    memory = document.get("worker_memory")
    hard_limit = memory.get("hard_limit") if isinstance(memory, Mapping) else None
    job = hard_limit.get("job_object") if isinstance(hard_limit, Mapping) else None
    projection = document.get("projection")
    scope = projection.get("scope") if isinstance(projection, Mapping) else None
    gate = (
        projection.get("projection_gate")
        if isinstance(projection, Mapping)
        else None
    )
    source_before = document.get("source_before")
    source_after = document.get("source_after")
    raw_characteristic = document.get("characteristic_length_m")
    characteristic_matches = (
        _positive_finite(raw_characteristic)
        and math.isclose(
            float(raw_characteristic),
            float(expected_characteristic_length_m),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
    )
    projected = gate.get("projected_3d_elements") if isinstance(gate, Mapping) else None
    source_integrity = (
        isinstance(source_before, Mapping)
        and isinstance(source_after, Mapping)
        and dict(source_before) == dict(source_after)
        and source_before.get("read_only") is True
        and _is_sha256(source_before.get("sha256"))
        and isinstance(source_before.get("size_bytes"), int)
        and not isinstance(source_before.get("size_bytes"), bool)
        and int(source_before["size_bytes"]) > 0
    )
    strict_pass = (
        document.get("schema") == PROJECTION_MANIFEST_SCHEMA
        and document.get("status") == "PASS"
        and document.get("projection_only") is True
        and document.get("calibration_PASS_authorized") is False
        and document.get("production") is False
        and document.get("mesh_written") is False
        and document.get("su2_called") is False
        and document.get("paraview_called") is False
        and document.get("external_commands") == []
        and document.get("error") is None
        and document.get("worker_error") is None
        and document.get("source_unchanged") is True
        and source_integrity
        and document.get("config_sha256") == expected_config_hash
        and document.get("coarse_contract_sha256") == expected_contract_hash
        and _matching_path(document.get("config_path"), expected_config)
        and _matching_path(document.get("output_directory"), output_directory)
        and characteristic_matches
        and isinstance(session, Mapping)
        and session.get("initialize_called") is True
        and session.get("finalize_called") is True
        and session.get("cleanup_errors") == []
        and isinstance(worker, Mapping)
        and worker.get("hard_limit_installed_before_heavy_import") is True
        and worker.get("heavy_dependencies_imported") is True
        and worker.get("projection_runner_called") is True
        and isinstance(memory, Mapping)
        and isinstance(hard_limit, Mapping)
        and hard_limit.get("status") == "PASS"
        and hard_limit.get("requested_process_memory_limit_bytes")
        == expected_process_memory_limit_bytes
        and isinstance(job, Mapping)
        and job.get("process_memory_limit_bytes")
        == expected_process_memory_limit_bytes
        and job.get("process_assigned") is True
        and job.get("handle_retained_for_process_lifetime") is True
        and isinstance(memory.get("process_after_projection"), Mapping)
        and memory["process_after_projection"].get("status") == "PASS"
        and isinstance(memory.get("system_after_projection"), Mapping)
        and memory["system_after_projection"].get("status") == "PASS"
        and isinstance(projection, Mapping)
        and projection.get("schema") == "cfdpipe.coarse_mesh_projection.v1"
        and projection.get("status") == "PASS"
        and isinstance(scope, Mapping)
        and scope.get("projection_only") is True
        and scope.get("calibration_PASS_authorized") is False
        and scope.get("production") is False
        and projection.get("full_layer_subdivision_performed") is False
        and projection.get("mesh_generated_in_memory") is True
        and projection.get("mesh_written") is False
        and projection.get("wall_surface_count") == 48
        and isinstance(gate, Mapping)
        and gate.get("status") == "PASS"
        and gate.get("strictly_below_cap") is True
        and gate.get("maximum_projected_3d_elements_exclusive")
        == PROJECTION_ELEMENT_CAP_EXCLUSIVE
        and isinstance(projected, int)
        and not isinstance(projected, bool)
        and 0 < projected < PROJECTION_ELEMENT_CAP_EXCLUSIVE
    )
    if not strict_pass:
        raise CoarseProjectionEvidenceError(
            "projection manifest is not a strict matching count-only PASS"
        )

    evidence: dict[str, Any] = {
        "schema": PROJECTION_EVIDENCE_SCHEMA,
        "status": "PASS",
        "path": str(resolved),
        "sha256": actual_sha256,
        "size_bytes": size_bytes,
        "output_directory": str(output_directory),
        "config_path": str(expected_config),
        "config_sha256": expected_config_hash,
        "coarse_contract_sha256": expected_contract_hash,
        "characteristic_length_m": float(raw_characteristic),
        "projected_3d_elements": int(projected),
        "strictly_below_300000": True,
        "source_sha256": str(source_before["sha256"]),
        "gmsh_finalize_called": True,
        "worker_hard_limit_bytes": expected_process_memory_limit_bytes,
        "worker_job_object_verified": True,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
    }
    evidence["evidence_sha256"] = _canonical_sha256(evidence)
    return evidence


def validate_projection_evidence_record(
    record: Mapping[str, Any],
    *,
    expected_path: str | os.PathLike[str],
    expected_sha256: str,
    expected_config_sha256: str,
    expected_coarse_contract_sha256: str,
    expected_characteristic_length_m: float,
    expected_projected_3d_elements: int,
) -> dict[str, Any]:
    """Validate an already-loaded evidence record embedded in another manifest."""

    if not isinstance(record, Mapping):
        raise CoarseProjectionEvidenceError("embedded projection evidence is missing")
    unsigned = dict(record)
    configured = unsigned.pop("evidence_sha256", None)
    try:
        expected_resolved = Path(expected_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise CoarseProjectionEvidenceError(
            "embedded projection source file no longer exists"
        ) from error
    characteristic = record.get("characteristic_length_m")
    projected = record.get("projected_3d_elements")
    valid = (
        record.get("schema") == PROJECTION_EVIDENCE_SCHEMA
        and record.get("status") == "PASS"
        and _is_sha256(configured)
        and _canonical_sha256(unsigned) == configured
        and _matching_path(record.get("path"), expected_resolved)
        and record.get("sha256") == expected_sha256
        and record.get("config_sha256") == expected_config_sha256
        and record.get("coarse_contract_sha256")
        == expected_coarse_contract_sha256
        and _positive_finite(characteristic)
        and math.isclose(
            float(characteristic),
            float(expected_characteristic_length_m),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
        and isinstance(projected, int)
        and not isinstance(projected, bool)
        and projected == expected_projected_3d_elements
        and 0 < projected < PROJECTION_ELEMENT_CAP_EXCLUSIVE
        and record.get("strictly_below_300000") is True
        and record.get("gmsh_finalize_called") is True
        and record.get("worker_job_object_verified") is True
        and record.get("mesh_written") is False
        and record.get("su2_called") is False
        and record.get("paraview_called") is False
    )
    if not valid:
        raise CoarseProjectionEvidenceError(
            "embedded projection evidence is stale or incomplete"
        )
    return dict(record)


__all__ = [
    "CoarseProjectionEvidenceError",
    "PROJECTION_ELEMENT_CAP_EXCLUSIVE",
    "PROJECTION_EVIDENCE_SCHEMA",
    "PROJECTION_MANIFEST_SCHEMA",
    "load_and_validate_projection_evidence",
    "validate_projection_evidence_record",
]
