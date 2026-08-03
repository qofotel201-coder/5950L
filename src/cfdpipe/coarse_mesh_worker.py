"""Hard-limited isolated worker for bounded coarse-mesh calibrations.

Only standard-library modules are imported at module load time.  The worker
validates the direct raw TOML resource policy, persists a fail-closed bootstrap
manifest, and installs its Windows Job Object memory ceiling before importing
the coarse-mesh, boundary-layer, or Gmsh-facing modules.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tomllib
import traceback
from typing import Any, Callable, Mapping, NamedTuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPOSITORY_ROOT / "runs" / "mesh" / "coarse"
WORKER_EVIDENCE_NAME = "worker_memory_limit.json"
WORKER_EVIDENCE_SCHEMA = "cfdpipe.coarse_mesh_calibration_worker.v1"

_RESOURCE_FIELDS = {
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


class CoarseCalibrationWorkerError(RuntimeError):
    """The calibration worker could not establish trustworthy isolation."""


class _HeavyDependencies(NamedTuple):
    normalize_coarse_mesh_config: Callable[..., dict[str, Any]]
    load_and_bind_local_schedule: Callable[..., dict[str, Any]]
    strategy_type: type
    build_coarse_calibration: Callable[..., dict[str, Any]]


class _MemoryApi(NamedTuple):
    capture_current_process_memory: Callable[..., dict[str, Any]]
    capture_system_physical_memory: Callable[..., dict[str, Any]]
    install_current_process_memory_limit: Callable[..., dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cfdpipe.coarse_mesh_worker",
        description="run one hard-limited Gmsh coarse-mesh calibration",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--coarse-contract-sha256", required=True)
    parser.add_argument("--characteristic-length", type=float, required=True)
    parser.add_argument("--projection-manifest", type=Path, required=True)
    parser.add_argument("--projection-sha256", required=True)
    parser.add_argument("--local-schedule", type=Path, required=True)
    parser.add_argument("--local-schedule-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _error_record(error: BaseException) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }
    evidence = getattr(error, "evidence", None)
    if isinstance(evidence, Mapping):
        record["evidence"] = dict(evidence)
    return record


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseCalibrationWorkerError(f"{label} must be a positive integer")
    pointer_maximum = (1 << (8 * struct.calcsize("P"))) - 1
    if value > pointer_maximum:
        raise CoarseCalibrationWorkerError(
            f"{label} exceeds the current platform size_t range"
        )
    return int(value)


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CoarseCalibrationWorkerError(f"{label} must be finite and positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseCalibrationWorkerError(
            f"{label} must be finite and positive"
        ) from error
    if not math.isfinite(result) or result <= 0.0:
        raise CoarseCalibrationWorkerError(f"{label} must be finite and positive")
    return result


def _direct_config(path: Path, repository_root: Path = REPOSITORY_ROOT) -> Path:
    lexical = path.expanduser()
    if ".." in lexical.parts:
        raise CoarseCalibrationWorkerError(
            "coarse worker config path must not contain '..'"
        )
    try:
        resolved = Path(os.path.abspath(lexical)).resolve(strict=True)
        expected = (repository_root / "config" / "coarse_mesh.toml").resolve(
            strict=True
        )
    except OSError as error:
        raise CoarseCalibrationWorkerError(
            "coarse worker config path cannot be resolved"
        ) from error
    if resolved != expected or not resolved.is_file() or lexical.is_symlink():
        raise CoarseCalibrationWorkerError(
            "coarse worker requires the direct config/coarse_mesh.toml file"
        )
    return resolved


def _new_output(
    path: Path,
    repository_root: Path = REPOSITORY_ROOT,
    output_root: Path = OUTPUT_ROOT,
) -> Path:
    lexical_input = path.expanduser()
    if ".." in lexical_input.parts:
        raise CoarseCalibrationWorkerError(
            "coarse worker output path must not contain '..'"
        )
    lexical = Path(os.path.abspath(lexical_input))
    root = output_root.resolve(strict=True)
    repository = repository_root.resolve(strict=True)
    resolved = lexical.resolve(strict=False)
    if (
        resolved == root
        or root not in resolved.parents
        or repository not in resolved.parents
        or lexical.exists()
        or resolved.exists()
        or lexical.is_symlink()
    ):
        raise CoarseCalibrationWorkerError(
            "coarse worker output must be a new directory below runs/mesh/coarse"
        )
    return lexical


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError as error:
        raise CoarseCalibrationWorkerError(
            f"cannot inspect local schedule path links: {error}"
        ) from error


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.casefold()
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoarseCalibrationWorkerError(
                f"local schedule JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CoarseCalibrationWorkerError(
        f"local schedule JSON contains non-finite constant {value}"
    )


def _validate_local_schedule_before_job(
    path: Path,
    expected_sha256: str,
    *,
    output: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate explicit schedule identity using only the standard library."""

    if not _is_sha256(expected_sha256):
        raise CoarseCalibrationWorkerError(
            "local schedule SHA-256 must be exactly 64 lowercase hexadecimal digits"
        )
    lexical_input = path.expanduser()
    if ".." in lexical_input.parts:
        raise CoarseCalibrationWorkerError(
            "local schedule path must not contain '..'"
        )
    lexical = Path(os.path.abspath(lexical_input))
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and _is_link_like(candidate):
            raise CoarseCalibrationWorkerError(
                "local schedule path must not contain a symbolic link or junction"
            )
    try:
        resolved = lexical.resolve(strict=True)
        root = output_root.resolve(strict=True)
    except OSError as error:
        raise CoarseCalibrationWorkerError(
            "explicit local schedule file does not exist"
        ) from error
    if (
        not resolved.is_file()
        or _is_link_like(resolved)
        or resolved.suffix.casefold() != ".json"
        or resolved == root
        or root not in resolved.parents
    ):
        raise CoarseCalibrationWorkerError(
            "local schedule must be an existing regular non-link JSON below runs/mesh/coarse"
        )
    resolved_output = output.resolve(strict=False)
    relative_output = resolved_output.relative_to(root)
    evidence_directory = root / "_worker_evidence" / relative_output
    if (
        resolved == resolved_output
        or resolved_output in resolved.parents
        or resolved == evidence_directory
        or evidence_directory in resolved.parents
    ):
        raise CoarseCalibrationWorkerError(
            "local schedule must not be inside this run output or worker evidence directory"
        )

    try:
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except OSError as error:
        raise CoarseCalibrationWorkerError(
            f"cannot read local schedule evidence: {error}"
        ) from error
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != before.st_size
    ):
        raise CoarseCalibrationWorkerError(
            "local schedule changed while it was being read"
        )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise CoarseCalibrationWorkerError(
            "local schedule SHA-256 does not exactly match the selected file"
        )
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseCalibrationWorkerError(
            f"local schedule is not valid strict UTF-8 JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise CoarseCalibrationWorkerError(
            "local schedule JSON root must be an object"
        )
    return {
        "path": str(resolved),
        "sha256": actual_sha256,
        "size_bytes": len(payload),
        "validated_before_job_object": True,
        "regular_non_link_json": True,
        "inside_runs_mesh_coarse": True,
        "outside_current_output_and_evidence": True,
    }


def _minimal_config(
    config_path: Path, characteristic_length_m: float
) -> tuple[dict[str, Any], dict[str, Any], int, float]:
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CoarseCalibrationWorkerError(
            f"cannot read coarse calibration config: {error}"
        ) from error
    if (
        document.get("schema") != "cfdpipe.coarse_mesh.v1"
        or document.get("status") != "CONFIGURED"
    ):
        raise CoarseCalibrationWorkerError(
            "coarse calibration config schema/status is invalid"
        )
    resource = document.get("resource")
    if not isinstance(resource, Mapping) or set(resource) != _RESOURCE_FIELDS:
        raise CoarseCalibrationWorkerError(
            "coarse calibration raw resource policy does not have the exact schema"
        )
    if (
        resource.get("physical_memory_required") is not True
        or resource.get("pagefile_cannot_rescue_physical_memory_failure") is not True
    ):
        raise CoarseCalibrationWorkerError(
            "coarse calibration config weakens the physical-memory policy"
        )
    minimum_samples = _positive_int(
        resource.get("minimum_calibration_samples"),
        "minimum calibration samples",
    )
    safety_factor = _positive_finite(
        resource.get("safety_factor"), "resource safety factor"
    )
    reserve = _positive_int(
        resource.get("os_physical_memory_reserve_bytes"),
        "OS physical-memory reserve",
    )
    minimum_disk = _positive_int(
        resource.get("minimum_disk_free_bytes"), "minimum disk free bytes"
    )
    projection_limit = _positive_int(
        resource.get("projection_worker_memory_limit_bytes"),
        "projection worker memory limit",
    )
    calibration_limit = _positive_int(
        resource.get("calibration_worker_memory_limit_bytes"),
        "calibration worker memory limit",
    )
    monitor_margin = _positive_int(
        resource.get("worker_monitor_margin_bytes"), "worker monitor margin"
    )
    if (
        minimum_samples < 2
        or safety_factor <= 1.0
        or reserve <= 0
        or minimum_disk <= 0
        or projection_limit > calibration_limit
        or monitor_margin <= 0
    ):
        raise CoarseCalibrationWorkerError(
            "coarse calibration raw resource policy is unsafe"
        )

    calibration = document.get("calibration")
    raw_lengths = (
        calibration.get("characteristic_lengths_m")
        if isinstance(calibration, Mapping)
        else None
    )
    if not isinstance(raw_lengths, list) or not raw_lengths:
        raise CoarseCalibrationWorkerError(
            "coarse calibration characteristic lengths are missing"
        )
    configured_lengths = [
        _positive_finite(item, "configured calibration characteristic length")
        for item in raw_lengths
    ]
    characteristic = _positive_finite(
        characteristic_length_m, "calibration characteristic length"
    )
    if not any(
        math.isclose(characteristic, configured, rel_tol=1.0e-12, abs_tol=0.0)
        for configured in configured_lengths
    ):
        raise CoarseCalibrationWorkerError(
            "calibration characteristic length is not authorized by the raw config"
        )
    return document, dict(resource), calibration_limit, characteristic


def _default_memory_api() -> _MemoryApi:
    module = importlib.import_module("cfdpipe.worker_memory")
    return _MemoryApi(
        module.capture_current_process_memory,
        module.capture_system_physical_memory,
        module.install_current_process_memory_limit,
    )


def _load_heavy_dependencies() -> _HeavyDependencies:
    """The import barrier; called only after hard-limit PASS evidence exists."""

    coarse = importlib.import_module("cfdpipe.coarse_mesh")
    boundary = importlib.import_module("cfdpipe.boundary_layer_smoke")
    schedule_binding = importlib.import_module(
        "cfdpipe.boundary_layer_schedule_binding"
    )
    return _HeavyDependencies(
        coarse.normalize_coarse_mesh_config,
        schedule_binding.load_and_bind_boundary_layer_local_schedule,
        boundary.RealProjectBoundaryLayerStrategy,
        coarse.build_coarse_calibration,
    )


def _require_memory_evidence(
    value: Any, label: str, *, requested_limit_bytes: int | None = None
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("status") != "PASS":
        raise CoarseCalibrationWorkerError(f"{label} did not return PASS evidence")
    result = dict(value)
    if requested_limit_bytes is not None:
        if result.get("requested_process_memory_limit_bytes") != requested_limit_bytes:
            raise CoarseCalibrationWorkerError(
                "installed calibration worker limit differs from the raw config"
            )
        job = result.get("job_object")
        if (
            not isinstance(job, Mapping)
            or job.get("process_memory_limit_bytes") != requested_limit_bytes
            or job.get("process_assigned") is not True
            or job.get("handle_retained_for_process_lifetime") is not True
        ):
            raise CoarseCalibrationWorkerError(
                "calibration worker memory-limit evidence is incomplete"
            )
    return result


def _evidence_path(output: Path, output_root: Path) -> Path:
    root = output_root.resolve(strict=True)
    resolved = output.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise CoarseCalibrationWorkerError(
            "worker evidence output escaped runs/mesh/coarse"
        ) from error
    evidence_directory = root / "_worker_evidence" / relative
    evidence_directory.mkdir(parents=True, exist_ok=True)
    if evidence_directory.is_symlink() or not evidence_directory.is_dir():
        raise CoarseCalibrationWorkerError(
            "worker evidence directory is not a direct directory"
        )
    path = evidence_directory / WORKER_EVIDENCE_NAME
    if path.exists() or path.is_symlink():
        raise CoarseCalibrationWorkerError(
            "worker memory evidence path already exists"
        )
    return path


def _manifest_skeleton(
    *,
    config_path: Path,
    output: Path,
    evidence_path: Path,
    characteristic_length_m: float,
    limit_bytes: int,
    local_schedule: Mapping[str, Any],
    config_sha256: str,
    coarse_contract_sha256: str,
    projection_evidence: Mapping[str, Any],
    argv: list[str],
) -> dict[str, Any]:
    return {
        "schema": WORKER_EVIDENCE_SCHEMA,
        "status": "FAIL",
        "calibration_only": True,
        "production_mesh_eligible": False,
        "su2_called": False,
        "paraview_called": False,
        "started_at_utc": _utc_now(),
        "ended_at_utc": None,
        "config_sha256": config_sha256,
        "coarse_contract_sha256": coarse_contract_sha256,
        "projection_evidence": dict(projection_evidence),
        "worker": {
            "argv": list(argv),
            "config_path": str(config_path),
            "output_directory": str(output),
            "evidence_path": str(evidence_path),
            "characteristic_length_m": characteristic_length_m,
            "configured_process_memory_limit_bytes": limit_bytes,
            "hard_limit_installed_before_heavy_import": False,
            "heavy_dependencies_imported": False,
            "normalized_resource_matches_raw": False,
            "local_schedule_bound_after_heavy_import": False,
            "builder_called": False,
        },
        "local_schedule": {
            "pre_job_validation": dict(local_schedule),
            "binding": None,
        },
        "worker_memory": {
            "system_before_limit": None,
            "process_before_limit": None,
            "hard_limit": None,
            "system_after_limit": None,
            "process_after_limit": None,
            "system_after_calibration": None,
            "process_after_calibration": None,
        },
        "worker_error": None,
    }


def _encoded_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_encoded_json(payload))
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_encoded_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _attach_worker_evidence(output: Path, wrapper: Mapping[str, Any]) -> None:
    manifest_path = output / "coarse_calibration_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return

    def reject_constant(value: str) -> None:
        raise ValueError(f"calibration manifest contains {value}")

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
    )
    if not isinstance(manifest, dict):
        raise CoarseCalibrationWorkerError(
            "coarse calibration manifest is not a JSON object"
        )
    manifest["worker_isolation"] = {
        "schema": wrapper.get("schema"),
        "status": wrapper.get("status"),
        "config_sha256": wrapper.get("config_sha256"),
        "coarse_contract_sha256": wrapper.get("coarse_contract_sha256"),
        "projection_evidence": wrapper.get("projection_evidence"),
        "worker": wrapper.get("worker"),
        "worker_memory": wrapper.get("worker_memory"),
        "local_schedule": wrapper.get("local_schedule"),
        "worker_error": wrapper.get("worker_error"),
    }
    _atomic_write_json(manifest_path, manifest)


def execute_calibration_worker(
    *,
    config_path: Path,
    config_sha256: str,
    coarse_contract_sha256: str,
    characteristic_length_m: float,
    projection_manifest_path: Path,
    projection_manifest_sha256: str,
    local_schedule_path: Path,
    local_schedule_sha256: str,
    output_directory: Path,
    argv: list[str],
    repository_root: Path = REPOSITORY_ROOT,
    output_root: Path = OUTPUT_ROOT,
    memory_api: _MemoryApi | None = None,
    heavy_loader: Callable[[], _HeavyDependencies] = _load_heavy_dependencies,
    projection_evidence_loader: Callable[..., dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute one calibration worker; injected APIs keep tests Gmsh-free."""

    output: Path | None = None
    evidence_path: Path | None = None
    wrapper: dict[str, Any] | None = None
    memory: _MemoryApi | None = None
    try:
        root = repository_root.resolve(strict=True)
        config = _direct_config(config_path, root)
        if not _is_sha256(config_sha256) or not _is_sha256(
            coarse_contract_sha256
        ):
            raise CoarseCalibrationWorkerError(
                "controller config/contract SHA-256 values are invalid"
            )
        if _sha256_file(config) != config_sha256:
            raise CoarseCalibrationWorkerError(
                "coarse config changed after controller preflight"
            )
        output = _new_output(output_directory, root, output_root)
        document, raw_resource, limit_bytes, characteristic = _minimal_config(
            config, characteristic_length_m
        )
        calibration_document = document.get("calibration")
        raw_projection_characteristic = (
            calibration_document.get("characteristic_lengths_m", [None])[0]
            if isinstance(calibration_document, Mapping)
            else None
        )
        projection_characteristic = _positive_finite(
            raw_projection_characteristic,
            "first configured projection characteristic length",
        )
        if projection_evidence_loader is None:
            projection_module = importlib.import_module(
                "cfdpipe.coarse_projection_evidence"
            )
            projection_loader = (
                projection_module.load_and_validate_projection_evidence
            )
        else:
            projection_loader = projection_evidence_loader
        projection_evidence = projection_loader(
            projection_manifest_path,
            projection_manifest_sha256,
            output_root=output_root,
            expected_config_path=config,
            expected_config_sha256=config_sha256,
            expected_coarse_contract_sha256=coarse_contract_sha256,
            expected_characteristic_length_m=projection_characteristic,
            expected_process_memory_limit_bytes=int(
                raw_resource["projection_worker_memory_limit_bytes"]
            ),
        )
        local_schedule = _validate_local_schedule_before_job(
            local_schedule_path,
            local_schedule_sha256,
            output=output,
            output_root=output_root,
        )
        evidence_path = _evidence_path(output, output_root)
        wrapper = _manifest_skeleton(
            config_path=config,
            output=output,
            evidence_path=evidence_path,
            characteristic_length_m=characteristic,
            limit_bytes=limit_bytes,
            local_schedule=local_schedule,
            config_sha256=config_sha256,
            coarse_contract_sha256=coarse_contract_sha256,
            projection_evidence=projection_evidence,
            argv=argv,
        )
        # Persist a FAIL skeleton before the Job call.  An OS hard kill or a
        # nested-Job failure therefore cannot look like a missing PASS record.
        _write_new_json(evidence_path, wrapper)

        memory = memory_api or _default_memory_api()
        wrapper["worker_memory"]["system_before_limit"] = _require_memory_evidence(
            memory.capture_system_physical_memory(), "system memory before limit"
        )
        wrapper["worker_memory"]["process_before_limit"] = _require_memory_evidence(
            memory.capture_current_process_memory(), "process memory before limit"
        )
        wrapper["worker_memory"]["hard_limit"] = _require_memory_evidence(
            memory.install_current_process_memory_limit(limit_bytes),
            "worker hard memory limit",
            requested_limit_bytes=limit_bytes,
        )
        wrapper["worker"]["hard_limit_installed_before_heavy_import"] = True
        wrapper["worker_memory"]["system_after_limit"] = _require_memory_evidence(
            memory.capture_system_physical_memory(), "system memory after limit"
        )
        wrapper["worker_memory"]["process_after_limit"] = _require_memory_evidence(
            memory.capture_current_process_memory(), "process memory after limit"
        )
        _atomic_write_json(evidence_path, wrapper)

        heavy = heavy_loader()
        wrapper["worker"]["heavy_dependencies_imported"] = True
        contract = heavy.normalize_coarse_mesh_config(
            document, repository_root=root
        )
        if contract.get("normalized_config_sha256") != coarse_contract_sha256:
            raise CoarseCalibrationWorkerError(
                "normalized coarse contract differs from controller preflight"
            )
        normalized_resource = contract.get("resource")
        if not isinstance(normalized_resource, Mapping) or dict(
            normalized_resource
        ) != raw_resource:
            raise CoarseCalibrationWorkerError(
                "normalized contract resource values differ from the raw TOML policy"
            )
        if (
            normalized_resource.get("calibration_worker_memory_limit_bytes")
            != limit_bytes
        ):
            raise CoarseCalibrationWorkerError(
                "normalized contract changed the installed calibration worker limit"
            )
        wrapper["worker"]["normalized_resource_matches_raw"] = True

        local_schedule_binding = heavy.load_and_bind_local_schedule(
            coarse_contract=contract,
            plan_path=local_schedule["path"],
            plan_sha256=local_schedule["sha256"],
        )
        if (
            not isinstance(local_schedule_binding, Mapping)
            or local_schedule_binding.get("schema")
            != "cfdpipe.boundary_layer_local_schedule_binding.v1"
            or local_schedule_binding.get("status") != "PASS"
            or local_schedule_binding.get("source_plan_path")
            != local_schedule["path"]
            or local_schedule_binding.get("source_plan_sha256")
            != local_schedule["sha256"]
            or local_schedule_binding.get("source_plan_size_bytes")
            != local_schedule["size_bytes"]
            or not _is_sha256(local_schedule_binding.get("binding_sha256"))
        ):
            raise CoarseCalibrationWorkerError(
                "local schedule binding did not preserve the selected path/SHA-256 evidence"
            )
        wrapper["worker"]["local_schedule_bound_after_heavy_import"] = True
        wrapper["local_schedule"]["binding"] = {
            "schema": local_schedule_binding["schema"],
            "status": local_schedule_binding["status"],
            "source_plan_path": local_schedule_binding["source_plan_path"],
            "source_plan_sha256": local_schedule_binding["source_plan_sha256"],
            "source_plan_size_bytes": local_schedule_binding[
                "source_plan_size_bytes"
            ],
            "coarse_contract_sha256": local_schedule_binding.get(
                "coarse_contract_sha256"
            ),
            "surface_count": local_schedule_binding.get("surface_count"),
            "layer_count": local_schedule_binding.get("layer_count"),
            "binding_sha256": local_schedule_binding["binding_sha256"],
        }

        configured_lengths = contract.get("calibration_characteristic_lengths_m")
        if not isinstance(configured_lengths, list) or not any(
            math.isclose(
                characteristic, float(value), rel_tol=1.0e-12, abs_tol=0.0
            )
            for value in configured_lengths
        ):
            raise CoarseCalibrationWorkerError(
                "normalized contract does not authorize the calibration length"
            )
        markers = tomllib.loads(
            (root / "config" / "markers.toml").read_text(encoding="utf-8")
        )
        topology = tomllib.loads(
            (root / "config" / "topology_smoke.toml").read_text(encoding="utf-8")
        )
        strategy = heavy.strategy_type(
            markers,
            topology,
            marker_config_sha256=contract["provenance"]["markers_sha256"],
            topology_smoke_config_sha256=contract["provenance"][
                "topology_smoke_sha256"
            ],
        )
        wrapper["worker"]["builder_called"] = True
        _atomic_write_json(evidence_path, wrapper)
        manifest = heavy.build_coarse_calibration(
            contract=contract,
            characteristic_length_m=characteristic,
            output_directory=output,
            allowed_output_root=output_root,
            strategy=strategy,
            local_schedule_binding=local_schedule_binding,
            projection_evidence=projection_evidence,
            controller_argv=argv,
        )
        wrapper["worker_memory"]["process_after_calibration"] = (
            _require_memory_evidence(
                memory.capture_current_process_memory(),
                "process memory after calibration",
            )
        )
        wrapper["worker_memory"]["system_after_calibration"] = (
            _require_memory_evidence(
                memory.capture_system_physical_memory(),
                "system memory after calibration",
            )
        )
        wrapper["status"] = "PASS" if manifest.get("status") == "PASS" else "FAIL"
        wrapper["ended_at_utc"] = _utc_now()
        _atomic_write_json(evidence_path, wrapper)
        _attach_worker_evidence(output, wrapper)
        manifest["worker_isolation"] = {
            "schema": wrapper["schema"],
            "status": wrapper["status"],
            "config_sha256": wrapper["config_sha256"],
            "coarse_contract_sha256": wrapper[
                "coarse_contract_sha256"
            ],
            "projection_evidence": wrapper["projection_evidence"],
            "worker": wrapper["worker"],
            "worker_memory": wrapper["worker_memory"],
            "local_schedule": wrapper["local_schedule"],
            "worker_error": None,
        }
        return (0 if wrapper["status"] == "PASS" else 1), manifest
    except BaseException as error:
        traceback.print_exception(type(error), error, error.__traceback__)
        if wrapper is not None and evidence_path is not None:
            wrapper["status"] = "FAIL"
            wrapper["ended_at_utc"] = _utc_now()
            wrapper["worker_error"] = _error_record(error)
            if memory is not None and wrapper["worker"][
                "hard_limit_installed_before_heavy_import"
            ]:
                try:
                    wrapper["worker_memory"]["process_after_calibration"] = (
                        _require_memory_evidence(
                            memory.capture_current_process_memory(),
                            "process memory after failed calibration",
                        )
                    )
                    wrapper["worker_memory"]["system_after_calibration"] = (
                        _require_memory_evidence(
                            memory.capture_system_physical_memory(),
                            "system memory after failed calibration",
                        )
                    )
                except BaseException as memory_error:
                    wrapper["worker_error"]["post_failure_memory_error"] = (
                        _error_record(memory_error)
                    )
            try:
                _atomic_write_json(evidence_path, wrapper)
            except BaseException as write_error:
                traceback.print_exception(
                    type(write_error), write_error, write_error.__traceback__
                )
            if output is not None:
                try:
                    _attach_worker_evidence(output, wrapper)
                except BaseException as attach_error:
                    traceback.print_exception(
                        type(attach_error), attach_error, attach_error.__traceback__
                    )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if wrapper is None:
            wrapper = {
                "schema": WORKER_EVIDENCE_SCHEMA,
                "status": "FAIL",
                "worker_error": _error_record(error),
            }
        return 1, wrapper


def _compact_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    worker = manifest.get("worker")
    error = manifest.get("worker_error")
    return {
        "status": manifest.get("status", "FAIL"),
        "evidence_path": (
            worker.get("evidence_path") if isinstance(worker, Mapping) else None
        ),
        "element_count_3d": (
            manifest.get("generation_audit", {}).get("element_count_3d")
            if isinstance(manifest.get("generation_audit"), Mapping)
            else None
        ),
        "peak_working_set_bytes": manifest.get("peak_working_set_bytes"),
        "elapsed_seconds": manifest.get("elapsed_seconds"),
        "error_type": error.get("type") if isinstance(error, Mapping) else None,
    }


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    controller_argv = [
        str(Path(sys.executable).resolve()),
        "-m",
        "cfdpipe.coarse_mesh_worker",
        *raw_argv,
    ]
    code, manifest = execute_calibration_worker(
        config_path=args.config,
        config_sha256=args.config_sha256,
        coarse_contract_sha256=args.coarse_contract_sha256,
        characteristic_length_m=args.characteristic_length,
        projection_manifest_path=args.projection_manifest,
        projection_manifest_sha256=args.projection_sha256,
        local_schedule_path=args.local_schedule,
        local_schedule_sha256=args.local_schedule_sha256,
        output_directory=args.output,
        argv=controller_argv,
    )
    print(
        json.dumps(
            _compact_summary(manifest),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CoarseCalibrationWorkerError",
    "WORKER_EVIDENCE_NAME",
    "WORKER_EVIDENCE_SCHEMA",
    "execute_calibration_worker",
    "main",
]
