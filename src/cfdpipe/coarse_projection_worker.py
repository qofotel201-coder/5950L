"""Memory-isolated worker for the one-layer coarse-mesh projection.

Only standard-library modules are imported at module load time.  The worker
validates its direct inputs, captures baseline memory evidence, and installs a
hard Windows per-process memory limit *before* importing the coarse-mesh,
boundary-layer, projection, or Gmsh-facing code.

The projection runner owns the Gmsh lifecycle and guarantees finalization.
This wrapper never tries to second-guess or duplicate that cleanup.
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
MANIFEST_NAME = "projection_manifest.json"
MANIFEST_SCHEMA = "cfdpipe.coarse_mesh_projection_manifest.v1"


class CoarseProjectionWorkerError(RuntimeError):
    """The isolated worker could not establish trustworthy projection evidence."""


class _HeavyDependencies(NamedTuple):
    normalize_coarse_mesh_config: Callable[..., dict[str, Any]]
    projection_runner: Callable[..., dict[str, Any]]
    strategy_type: type


class _MemoryApi(NamedTuple):
    capture_current_process_memory: Callable[..., dict[str, Any]]
    capture_system_physical_memory: Callable[..., dict[str, Any]]
    install_current_process_memory_limit: Callable[..., dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cfdpipe.coarse_projection_worker",
        description="run one hard-limited Gmsh coarse-mesh count projection",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--coarse-contract-sha256", required=True)
    parser.add_argument("--characteristic-length", type=float, required=True)
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
        raise CoarseProjectionWorkerError(f"{label} must be a positive integer")
    pointer_maximum = (1 << (8 * struct.calcsize("P"))) - 1
    if value > pointer_maximum:
        raise CoarseProjectionWorkerError(
            f"{label} exceeds the current platform size_t range"
        )
    return int(value)


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CoarseProjectionWorkerError(f"{label} must be finite and positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseProjectionWorkerError(
            f"{label} must be finite and positive"
        ) from error
    if not math.isfinite(result) or result <= 0.0:
        raise CoarseProjectionWorkerError(f"{label} must be finite and positive")
    return result


def _required_sha256(value: Any, label: str) -> str:
    candidate = str(value)
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise CoarseProjectionWorkerError(
            f"{label} must be 64 lowercase hexadecimal digits"
        )
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _direct_config(path: Path, repository_root: Path) -> Path:
    lexical = path.expanduser()
    if ".." in lexical.parts:
        raise CoarseProjectionWorkerError(
            "projection worker config path must not contain '..'"
        )
    try:
        resolved = Path(os.path.abspath(lexical)).resolve(strict=True)
        expected = (repository_root / "config" / "coarse_mesh.toml").resolve(
            strict=True
        )
    except OSError as error:
        raise CoarseProjectionWorkerError(
            "projection worker config path cannot be resolved"
        ) from error
    if resolved != expected or not resolved.is_file() or lexical.is_symlink():
        raise CoarseProjectionWorkerError(
            "projection worker requires the direct config/coarse_mesh.toml file"
        )
    return resolved


def _new_output(path: Path, repository_root: Path, output_root: Path) -> Path:
    lexical = Path(os.path.abspath(path.expanduser()))
    if ".." in path.expanduser().parts:
        raise CoarseProjectionWorkerError(
            "projection worker output path must not contain '..'"
        )
    root = output_root.resolve(strict=True)
    repository = repository_root.resolve(strict=True)
    resolved = lexical.resolve(strict=False)
    if (
        resolved == root
        or root not in resolved.parents
        or repository not in resolved.parents
        or lexical.exists()
        or resolved.exists()
    ):
        raise CoarseProjectionWorkerError(
            "projection worker output must be a new directory below runs/mesh/coarse"
        )
    parent = lexical.parent.resolve(strict=True)
    if parent != root or parent.is_symlink():
        raise CoarseProjectionWorkerError(
            "projection worker output must be a direct child of runs/mesh/coarse"
        )
    return lexical


def _minimal_config(
    config_path: Path, characteristic_length_m: float
) -> tuple[dict[str, Any], int, float]:
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CoarseProjectionWorkerError(
            f"cannot read coarse projection config: {error}"
        ) from error
    if (
        document.get("schema") != "cfdpipe.coarse_mesh.v1"
        or document.get("status") != "CONFIGURED"
    ):
        raise CoarseProjectionWorkerError(
            "coarse projection config schema/status is invalid"
        )
    resource = document.get("resource")
    if not isinstance(resource, Mapping):
        raise CoarseProjectionWorkerError(
            "coarse projection config resource policy is missing"
        )
    if (
        resource.get("physical_memory_required") is not True
        or resource.get("pagefile_cannot_rescue_physical_memory_failure") is not True
    ):
        raise CoarseProjectionWorkerError(
            "coarse projection config weakens the physical-memory policy"
        )
    limit_bytes = _positive_int(
        resource.get("projection_worker_memory_limit_bytes"),
        "projection worker memory limit",
    )
    calibration = document.get("calibration")
    raw_lengths = (
        calibration.get("characteristic_lengths_m")
        if isinstance(calibration, Mapping)
        else None
    )
    if not isinstance(raw_lengths, list) or not raw_lengths:
        raise CoarseProjectionWorkerError(
            "coarse projection calibration lengths are missing"
        )
    configured_lengths = [
        _positive_finite(item, "configured calibration characteristic length")
        for item in raw_lengths
    ]
    characteristic = _positive_finite(
        characteristic_length_m, "projection characteristic length"
    )
    if not any(
        math.isclose(characteristic, configured, rel_tol=1.0e-12, abs_tol=0.0)
        for configured in configured_lengths
    ):
        raise CoarseProjectionWorkerError(
            "projection characteristic length is not authorized by the config"
        )
    return document, limit_bytes, characteristic


def _default_memory_api() -> _MemoryApi:
    # worker_memory is standard-library-only, but keep even this import after
    # direct path/config validation so invalid invocations stay side-effect free.
    module = importlib.import_module("cfdpipe.worker_memory")
    return _MemoryApi(
        module.capture_current_process_memory,
        module.capture_system_physical_memory,
        module.install_current_process_memory_limit,
    )


def _load_heavy_dependencies() -> _HeavyDependencies:
    # This is the intentional import barrier.  It must run only after the Job
    # Object hard limit is installed and evidenced.
    coarse = importlib.import_module("cfdpipe.coarse_mesh")
    projection = importlib.import_module("cfdpipe.coarse_mesh_projection")
    boundary = importlib.import_module("cfdpipe.boundary_layer_smoke")
    return _HeavyDependencies(
        coarse.normalize_coarse_mesh_config,
        projection.run_coarse_mesh_projection,
        boundary.RealProjectBoundaryLayerStrategy,
    )


def _require_memory_evidence(
    value: Any, label: str, *, requested_limit_bytes: int | None = None
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("status") != "PASS":
        raise CoarseProjectionWorkerError(f"{label} did not return PASS evidence")
    result = dict(value)
    if requested_limit_bytes is not None:
        if result.get("requested_process_memory_limit_bytes") != requested_limit_bytes:
            raise CoarseProjectionWorkerError(
                "installed worker memory limit differs from the config"
            )
        job = result.get("job_object")
        if (
            not isinstance(job, Mapping)
            or job.get("process_memory_limit_bytes") != requested_limit_bytes
            or job.get("process_assigned") is not True
            or job.get("handle_retained_for_process_lifetime") is not True
        ):
            raise CoarseProjectionWorkerError(
                "worker memory-limit evidence is incomplete"
            )
    return result


def _manifest_skeleton(
    *,
    config_path: Path,
    output: Path,
    characteristic_length_m: float,
    limit_bytes: int,
    config_sha256: str,
    coarse_contract_sha256: str,
    argv: list[str],
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "status": "FAIL",
        "projection_only": True,
        "calibration_PASS_authorized": False,
        "production": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "started_at_utc": _utc_now(),
        "ended_at_utc": None,
        "config_path": str(config_path),
        "config_sha256": config_sha256,
        "coarse_contract_sha256": coarse_contract_sha256,
        "characteristic_length_m": characteristic_length_m,
        "output_directory": str(output),
        "worker": {
            "argv": list(argv),
            "config_path": str(config_path),
            "output_directory": str(output),
            "manifest_path": str(output / MANIFEST_NAME),
            "characteristic_length_m": characteristic_length_m,
            "configured_process_memory_limit_bytes": limit_bytes,
            "hard_limit_installed_before_heavy_import": False,
            "heavy_dependencies_imported": False,
            "projection_runner_called": False,
        },
        "worker_memory": {
            "system_before_limit": None,
            "process_before_limit": None,
            "hard_limit": None,
            "system_after_limit": None,
            "process_after_limit": None,
            "system_after_projection": None,
            "process_after_projection": None,
        },
        "worker_error": None,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _merge_projection_manifest(
    wrapper: dict[str, Any], projection: Any
) -> dict[str, Any]:
    if not isinstance(projection, Mapping):
        raise CoarseProjectionWorkerError(
            "projection runner did not return a manifest object"
        )
    if (
        projection.get("schema") != MANIFEST_SCHEMA
        or projection.get("projection_only") is not True
        or projection.get("calibration_PASS_authorized") is not False
        or projection.get("production") is not False
        or projection.get("mesh_written") is not False
        or projection.get("su2_called") is not False
        or projection.get("paraview_called") is not False
        or projection.get("external_commands") != []
    ):
        raise CoarseProjectionWorkerError(
            "projection runner manifest violates projection-only scope"
        )
    worker = wrapper["worker"]
    memory = wrapper["worker_memory"]
    result = dict(projection)
    for key in (
        "config_path",
        "config_sha256",
        "coarse_contract_sha256",
        "characteristic_length_m",
        "output_directory",
    ):
        result[key] = wrapper[key]
    result["worker"] = worker
    result["worker_memory"] = memory
    result["worker_error"] = wrapper.get("worker_error")
    return result


def _compact_summary(manifest: Mapping[str, Any], path: Path) -> dict[str, Any]:
    projection = manifest.get("projection")
    gate = projection.get("projection_gate") if isinstance(projection, Mapping) else None
    return {
        "status": manifest.get("status", "FAIL"),
        "manifest_path": str(path),
        "projected_3d_elements": (
            gate.get("projected_3d_elements") if isinstance(gate, Mapping) else None
        ),
        "error_type": (
            manifest.get("worker_error", {}).get("type")
            if isinstance(manifest.get("worker_error"), Mapping)
            else manifest.get("error", {}).get("type")
            if isinstance(manifest.get("error"), Mapping)
            else None
        ),
    }


def execute_projection_worker(
    *,
    config_path: Path,
    config_sha256: str,
    coarse_contract_sha256: str,
    characteristic_length_m: float,
    output_directory: Path,
    argv: list[str],
    repository_root: Path = REPOSITORY_ROOT,
    output_root: Path = OUTPUT_ROOT,
    memory_api: _MemoryApi | None = None,
    heavy_loader: Callable[[], _HeavyDependencies] = _load_heavy_dependencies,
) -> tuple[int, dict[str, Any]]:
    """Execute one projection worker; dependency injection keeps tests Gmsh-free."""

    output: Path | None = None
    manifest_path: Path | None = None
    manifest: dict[str, Any] | None = None
    try:
        root = repository_root.resolve(strict=True)
        config = _direct_config(config_path, root)
        expected_config_sha256 = _required_sha256(
            config_sha256, "projection config SHA-256"
        )
        expected_contract_sha256 = _required_sha256(
            coarse_contract_sha256, "projection coarse contract SHA-256"
        )
        if _sha256(config) != expected_config_sha256:
            raise CoarseProjectionWorkerError(
                "projection config changed after controller preflight"
            )
        output = _new_output(output_directory, root, output_root)
        document, limit_bytes, characteristic = _minimal_config(
            config, characteristic_length_m
        )
        output.mkdir(parents=False, exist_ok=False)
        manifest_path = output / MANIFEST_NAME
        manifest = _manifest_skeleton(
            config_path=config,
            output=output,
            characteristic_length_m=characteristic,
            limit_bytes=limit_bytes,
            config_sha256=expected_config_sha256,
            coarse_contract_sha256=expected_contract_sha256,
            argv=argv,
        )

        memory = memory_api or _default_memory_api()
        manifest["worker_memory"]["system_before_limit"] = _require_memory_evidence(
            memory.capture_system_physical_memory(), "system memory before limit"
        )
        manifest["worker_memory"]["process_before_limit"] = _require_memory_evidence(
            memory.capture_current_process_memory(), "process memory before limit"
        )
        limit_evidence = _require_memory_evidence(
            memory.install_current_process_memory_limit(limit_bytes),
            "worker hard memory limit",
            requested_limit_bytes=limit_bytes,
        )
        manifest["worker_memory"]["hard_limit"] = limit_evidence
        manifest["worker"]["hard_limit_installed_before_heavy_import"] = True
        manifest["worker_memory"]["system_after_limit"] = _require_memory_evidence(
            memory.capture_system_physical_memory(), "system memory after limit"
        )
        manifest["worker_memory"]["process_after_limit"] = _require_memory_evidence(
            memory.capture_current_process_memory(), "process memory after limit"
        )
        # Persist proof of the installed limit before any heavy import.  If the
        # OS later kills the worker at the hard limit, the parent still has this
        # fail-closed evidence instead of a missing/ambiguous artifact.
        _atomic_write_json(manifest_path, manifest)

        heavy = heavy_loader()
        manifest["worker"]["heavy_dependencies_imported"] = True
        contract = heavy.normalize_coarse_mesh_config(
            document, repository_root=root
        )
        if contract.get("normalized_config_sha256") != expected_contract_sha256:
            raise CoarseProjectionWorkerError(
                "normalized projection contract differs from controller preflight"
            )
        normalized_resource = contract.get("resource")
        if (
            not isinstance(normalized_resource, Mapping)
            or normalized_resource.get("projection_worker_memory_limit_bytes")
            != limit_bytes
        ):
            raise CoarseProjectionWorkerError(
                "normalized contract changed the installed projection worker memory limit"
            )
        configured_lengths = contract.get("calibration_characteristic_lengths_m")
        if not isinstance(configured_lengths, list) or not any(
            math.isclose(
                characteristic, float(value), rel_tol=1.0e-12, abs_tol=0.0
            )
            for value in configured_lengths
        ):
            raise CoarseProjectionWorkerError(
                "normalized contract does not authorize the projection characteristic length"
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
        manifest["worker"]["projection_runner_called"] = True
        projection = heavy.projection_runner(
            contract=contract,
            characteristic_length_m=characteristic,
            strategy=strategy,
        )
        manifest = _merge_projection_manifest(manifest, projection)
        manifest["worker_memory"]["process_after_projection"] = (
            _require_memory_evidence(
                memory.capture_current_process_memory(),
                "process memory after projection",
            )
        )
        manifest["worker_memory"]["system_after_projection"] = (
            _require_memory_evidence(
                memory.capture_system_physical_memory(),
                "system memory after projection",
            )
        )
        manifest["ended_at_utc"] = manifest.get("ended_at_utc") or _utc_now()
        _atomic_write_json(manifest_path, manifest)
        return (0 if manifest.get("status") == "PASS" else 1), manifest
    except BaseException as error:
        traceback.print_exception(type(error), error, error.__traceback__)
        if manifest is not None and manifest_path is not None:
            manifest["status"] = "FAIL"
            manifest["ended_at_utc"] = _utc_now()
            manifest["worker_error"] = _error_record(error)
            try:
                _atomic_write_json(manifest_path, manifest)
            except BaseException as write_error:
                traceback.print_exception(
                    type(write_error), write_error, write_error.__traceback__
                )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if manifest is None:
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "status": "FAIL",
                "projection_only": True,
                "worker_error": _error_record(error),
            }
        return 1, manifest


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    controller_argv = [
        str(Path(sys.executable).resolve()),
        "-m",
        "cfdpipe.coarse_projection_worker",
        *raw_argv,
    ]
    code, manifest = execute_projection_worker(
        config_path=args.config,
        config_sha256=args.config_sha256,
        coarse_contract_sha256=args.coarse_contract_sha256,
        characteristic_length_m=args.characteristic_length,
        output_directory=args.output,
        argv=controller_argv,
    )
    output = Path(str(manifest.get("worker", {}).get("manifest_path", "")))
    if not output.name:
        output = args.output / MANIFEST_NAME
    print(
        json.dumps(
            _compact_summary(manifest, output),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CoarseProjectionWorkerError",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "execute_projection_worker",
    "main",
]
