"""Hard-limited worker for the coarse repair AUDIT_ONLY discovery path.

Only standard-library modules and the standard-library-only calibration worker
helpers are imported before the Windows Job Object limit is installed.  Heavy
Gmsh-facing modules are loaded only after strict projection and local-schedule
lineage has been checked and fail-closed memory evidence has been persisted.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import signal
import sys
import tomllib
import traceback
from typing import Any, Callable, Mapping, NamedTuple

from . import coarse_mesh_worker as _base


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = REPOSITORY_ROOT / "runs" / "mesh" / "coarse"
MANIFEST_NAME = "coarse_repair_audit_manifest.json"
WORKER_EVIDENCE_NAME = "repair_audit_worker.json"
WORKER_EVIDENCE_SCHEMA = "cfdpipe.coarse_repair_audit_worker.v1"
PHYSICAL_HOMOTOPY_REQUEST = (
    "physical-schedule-fixed-direction-homotopy-audit"
)
PHYSICAL_HOMOTOPY_PURPOSE = (
    "physical_local_schedule_fixed_direction_homotopy"
)
SCHEDULE_CONTINUATION_REQUEST = (
    "physical-schedule-first-frontier-direction-audit"
)
SCHEDULE_CONTINUATION_PURPOSE = (
    "physical_schedule_first_frontier_direction_continuation"
)


class CoarseRepairAuditWorkerError(RuntimeError):
    """The isolated repair-audit worker failed a mandatory safety gate."""


class CoarseRepairAuditWorkerCancelled(BaseException):
    """A Windows console break requested a controlled worker shutdown."""


class _HeavyDependencies(NamedTuple):
    normalize_coarse_mesh_config: Callable[..., dict[str, Any]]
    load_and_bind_local_schedule: Callable[..., dict[str, Any]]
    strategy_type: type
    audit_runner: Callable[..., dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _install_controlled_sigbreak_handler(
    *,
    platform: str | None = None,
    signal_module: Any = signal,
) -> Callable[[], None] | None:
    """Install an idempotently restorable Windows controlled-cancel handler.

    The handler intentionally performs no cleanup or I/O.  Its dedicated
    ``BaseException`` is allowed to unwind through the Gmsh-facing audit's
    existing ``finally`` blocks before this worker records failure evidence.
    """

    platform_name = sys.platform if platform is None else platform
    sigbreak = getattr(signal_module, "SIGBREAK", None)
    if platform_name != "win32" or sigbreak is None:
        return None
    previous_handler = signal_module.getsignal(sigbreak)
    cancellation_requested = False

    def controlled_cancel(signum: int, _frame: Any) -> None:
        nonlocal cancellation_requested
        if cancellation_requested:
            return
        cancellation_requested = True
        raise CoarseRepairAuditWorkerCancelled(
            f"coarse repair audit cancelled by Windows SIGBREAK ({signum})"
        )

    signal_module.signal(sigbreak, controlled_cancel)
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        signal_module.signal(sigbreak, previous_handler)
        restored = True

    return restore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cfdpipe.coarse_repair_audit_worker",
        description="run one hard-limited in-memory coarse repair audit",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--coarse-contract-sha256", required=True)
    parser.add_argument("--characteristic-length", type=float, required=True)
    parser.add_argument("--projection-manifest", type=Path, required=True)
    parser.add_argument("--projection-sha256", required=True)
    parser.add_argument("--local-schedule", type=Path, required=True)
    parser.add_argument("--local-schedule-sha256", required=True)
    parser.add_argument(
        "--schedule-feasibility-endpoint",
        choices=(
            "minimum-growth-existing-direction-repair",
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
            PHYSICAL_HOMOTOPY_REQUEST,
            SCHEDULE_CONTINUATION_REQUEST,
        ),
    )
    parser.add_argument("--direction-audit-primary", type=Path)
    parser.add_argument("--direction-audit-primary-sha256")
    parser.add_argument("--direction-audit-confirmation", type=Path)
    parser.add_argument("--direction-audit-confirmation-sha256")
    parser.add_argument("--direction-replay-approval-sha256")
    parser.add_argument("--physical-schedule-homotopy-endpoint-sha256")
    parser.add_argument("--homotopy-audit-manifest", type=Path)
    parser.add_argument("--homotopy-audit-manifest-sha256")
    parser.add_argument("--schedule-direction-continuation-endpoint-sha256")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _error_record(error: BaseException) -> dict[str, Any]:
    record = {
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


def _load_heavy_dependencies() -> _HeavyDependencies:
    coarse = importlib.import_module("cfdpipe.coarse_mesh")
    schedule = importlib.import_module("cfdpipe.boundary_layer_schedule_binding")
    boundary = importlib.import_module("cfdpipe.boundary_layer_smoke")
    audit = importlib.import_module("cfdpipe.coarse_repair_audit")
    return _HeavyDependencies(
        coarse.normalize_coarse_mesh_config,
        schedule.load_and_bind_boundary_layer_local_schedule,
        boundary.RealProjectBoundaryLayerStrategy,
        audit.run_coarse_repair_audit,
    )


def _evidence_path(output: Path, output_root: Path) -> Path:
    root = output_root.resolve(strict=True)
    resolved = output.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise CoarseRepairAuditWorkerError(
            "repair audit worker output escaped runs/mesh/coarse"
        ) from error
    directory = root / "_worker_evidence" / relative
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise CoarseRepairAuditWorkerError(
            "repair audit worker evidence directory is unsafe"
        )
    path = directory / WORKER_EVIDENCE_NAME
    if path.exists() or path.is_symlink():
        raise CoarseRepairAuditWorkerError(
            "repair audit worker evidence already exists"
        )
    return path


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _wrapper_skeleton(
    *,
    config: Path,
    output: Path,
    evidence_path: Path,
    characteristic: float,
    limit_bytes: int,
    config_sha256: str,
    contract_sha256: str,
    projection_evidence: Mapping[str, Any],
    local_schedule: Mapping[str, Any],
    direction_replay_sources: list[Mapping[str, Any]] | None,
    argv: list[str],
) -> dict[str, Any]:
    return {
        "schema": WORKER_EVIDENCE_SCHEMA,
        "status": "FAIL",
        "audit_only": True,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "started_at_utc": _utc_now(),
        "ended_at_utc": None,
        "config_sha256": config_sha256,
        "coarse_contract_sha256": contract_sha256,
        "projection_evidence": dict(projection_evidence),
        "local_schedule": {
            "pre_job_validation": dict(local_schedule),
            "binding": None,
        },
        "direction_replay": {
            "pre_job_sources": copy.deepcopy(direction_replay_sources),
            "approval": None,
            "homotopy_endpoint": None,
            "homotopy_audit_source": None,
            "schedule_direction_continuation_endpoint": None,
        },
        "worker": {
            "argv": list(argv),
            "config_path": str(config),
            "output_directory": str(output),
            "manifest_path": str(output / MANIFEST_NAME),
            "evidence_path": str(evidence_path),
            "characteristic_length_m": characteristic,
            "configured_process_memory_limit_bytes": limit_bytes,
            "hard_limit_installed_before_heavy_import": False,
            "heavy_dependencies_imported": False,
            "normalized_resource_matches_raw": False,
            "local_schedule_bound_after_heavy_import": False,
            "audit_runner_called": False,
        },
        "worker_memory": {
            "system_before_limit": None,
            "process_before_limit": None,
            "hard_limit": None,
            "system_after_limit": None,
            "process_after_limit": None,
            "system_after_audit": None,
            "process_after_audit": None,
        },
        "audit_result_status": None,
        "worker_error": None,
    }


def _is_sha256(value: Any) -> bool:
    return _base._is_sha256(value)


def _validate_direction_audit_before_job(
    path: Path,
    expected_sha256: str,
    *,
    output_root: Path,
) -> dict[str, Any]:
    if not _is_sha256(expected_sha256):
        raise CoarseRepairAuditWorkerError(
            "direction audit SHA-256 must be lowercase hexadecimal"
        )
    raw = path.expanduser()
    if ".." in raw.parts:
        raise CoarseRepairAuditWorkerError(
            "direction audit path must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and _base._is_link_like(candidate):
            raise CoarseRepairAuditWorkerError(
                "direction audit path contains a link or junction"
            )
    resolved = lexical.resolve(strict=True)
    root = output_root.resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.name != MANIFEST_NAME
        or root not in resolved.parents
        or "_worker_evidence" in {
            item.casefold() for item in resolved.relative_to(root).parts
        }
        or _base._sha256_file(resolved) != expected_sha256
    ):
        raise CoarseRepairAuditWorkerError(
            "direction audit file identity is invalid"
        )
    return {
        "path": str(resolved),
        "sha256": expected_sha256,
        "size_bytes": resolved.stat().st_size,
    }


def _validate_homotopy_audit_before_job(
    path: Path,
    expected_sha256: str,
    *,
    output_root: Path,
) -> dict[str, Any]:
    if not _is_sha256(expected_sha256):
        raise CoarseRepairAuditWorkerError(
            "homotopy audit SHA-256 must be lowercase hexadecimal"
        )
    raw = path.expanduser()
    if ".." in raw.parts:
        raise CoarseRepairAuditWorkerError(
            "homotopy audit path must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and _base._is_link_like(candidate):
            raise CoarseRepairAuditWorkerError(
                "homotopy audit path contains a link or junction"
            )
    resolved = lexical.resolve(strict=True)
    root = output_root.resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.name != MANIFEST_NAME
        or resolved.parent.parent != root
        or "_worker_evidence" in {
            item.casefold() for item in resolved.relative_to(root).parts
        }
        or _base._sha256_file(resolved) != expected_sha256
    ):
        raise CoarseRepairAuditWorkerError(
            "homotopy audit file identity is invalid"
        )
    return {
        "path": str(resolved),
        "sha256": expected_sha256,
        "size_bytes": resolved.stat().st_size,
    }


def execute_repair_audit_worker(
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
    schedule_feasibility_endpoint: str | None = None,
    direction_audit_primary_path: Path | None = None,
    direction_audit_primary_sha256: str | None = None,
    direction_audit_confirmation_path: Path | None = None,
    direction_audit_confirmation_sha256: str | None = None,
    direction_replay_approval_sha256: str | None = None,
    physical_schedule_homotopy_endpoint_sha256: str | None = None,
    homotopy_audit_manifest_path: Path | None = None,
    homotopy_audit_manifest_sha256: str | None = None,
    schedule_direction_continuation_endpoint_sha256: str | None = None,
    repository_root: Path = REPOSITORY_ROOT,
    output_root: Path = OUTPUT_ROOT,
    memory_api: _base._MemoryApi | None = None,
    heavy_loader: Callable[[], _HeavyDependencies] = _load_heavy_dependencies,
    projection_evidence_loader: Callable[..., dict[str, Any]] | None = None,
    sigbreak_installer: Callable[
        [], Callable[[], None] | None
    ] = _install_controlled_sigbreak_handler,
) -> tuple[int, dict[str, Any]]:
    """Execute one audit; dependency injection keeps all unit tests Gmsh-free."""

    output: Path | None = None
    evidence_path: Path | None = None
    wrapper: dict[str, Any] | None = None
    memory: _base._MemoryApi | None = None
    manifest: dict[str, Any] | None = None
    restore_sigbreak: Callable[[], None] | None = None
    try:
        if schedule_feasibility_endpoint not in (
            None,
            "minimum-growth-existing-direction-repair",
            "minimum-growth-owner-free-component-direction-audit",
            "minimum-growth-owner-free-component-pattern-audit",
            PHYSICAL_HOMOTOPY_REQUEST,
            SCHEDULE_CONTINUATION_REQUEST,
        ):
            raise CoarseRepairAuditWorkerError(
                "unsupported repair-audit schedule feasibility endpoint"
            )
        replay_values = (
            direction_audit_primary_path,
            direction_audit_primary_sha256,
            direction_audit_confirmation_path,
            direction_audit_confirmation_sha256,
            direction_replay_approval_sha256,
        )
        replay_requested = any(value is not None for value in replay_values)
        homotopy_requested = (
            schedule_feasibility_endpoint == PHYSICAL_HOMOTOPY_REQUEST
        )
        continuation_requested = (
            schedule_feasibility_endpoint == SCHEDULE_CONTINUATION_REQUEST
        )
        continuation_discovery_schema: str | None = None
        combined_replay_requested = homotopy_requested or continuation_requested
        if replay_requested and (
            any(value is None for value in replay_values)
            or (
                schedule_feasibility_endpoint is not None
                and not combined_replay_requested
            )
            or not _is_sha256(direction_replay_approval_sha256)
        ):
            raise CoarseRepairAuditWorkerError(
                "fixed-direction replay inputs must be complete and exclusive"
            )
        if (
            combined_replay_requested
            and not replay_requested
            or combined_replay_requested
            and not _is_sha256(physical_schedule_homotopy_endpoint_sha256)
            or not combined_replay_requested
            and physical_schedule_homotopy_endpoint_sha256 is not None
        ):
            raise CoarseRepairAuditWorkerError(
                "physical-schedule homotopy endpoint inputs are incomplete"
            )
        continuation_values = (
            homotopy_audit_manifest_path,
            homotopy_audit_manifest_sha256,
            schedule_direction_continuation_endpoint_sha256,
        )
        if (
            continuation_requested
            and (
                any(value is None for value in continuation_values)
                or not _is_sha256(homotopy_audit_manifest_sha256)
                or not _is_sha256(
                    schedule_direction_continuation_endpoint_sha256
                )
            )
            or not continuation_requested
            and any(value is not None for value in continuation_values)
        ):
            raise CoarseRepairAuditWorkerError(
                "schedule direction continuation inputs are incomplete"
            )
        root = repository_root.resolve(strict=True)
        config = _base._direct_config(config_path, root)
        if not _is_sha256(config_sha256) or not _is_sha256(
            coarse_contract_sha256
        ):
            raise CoarseRepairAuditWorkerError(
                "controller config/contract SHA-256 values are invalid"
            )
        if _base._sha256_file(config) != config_sha256:
            raise CoarseRepairAuditWorkerError(
                "coarse config changed after controller preflight"
            )
        output = _base._new_output(output_directory, root, output_root)
        document, raw_resource, limit_bytes, characteristic = _base._minimal_config(
            config, characteristic_length_m
        )
        calibration = document.get("calibration")
        raw_lengths = (
            calibration.get("characteristic_lengths_m")
            if isinstance(calibration, Mapping)
            else None
        )
        if (
            not isinstance(raw_lengths, list)
            or not raw_lengths
            or not math.isclose(
                characteristic,
                float(raw_lengths[0]),
                rel_tol=1.0e-12,
                abs_tol=0.0,
            )
        ):
            raise CoarseRepairAuditWorkerError(
                "repair audit is restricted to the first configured point"
            )
        if projection_evidence_loader is None:
            projection = importlib.import_module(
                "cfdpipe.coarse_projection_evidence"
            )
            projection_loader = projection.load_and_validate_projection_evidence
        else:
            projection_loader = projection_evidence_loader
        projection_evidence = projection_loader(
            projection_manifest_path,
            projection_manifest_sha256,
            output_root=output_root,
            expected_config_path=config,
            expected_config_sha256=config_sha256,
            expected_coarse_contract_sha256=coarse_contract_sha256,
            expected_characteristic_length_m=characteristic,
            expected_process_memory_limit_bytes=int(
                raw_resource["projection_worker_memory_limit_bytes"]
            ),
        )
        local_schedule = _base._validate_local_schedule_before_job(
            local_schedule_path,
            local_schedule_sha256,
            output=output,
            output_root=output_root,
        )
        direction_replay_sources = None
        homotopy_audit_source = None
        if replay_requested:
            direction_replay_sources = [
                _validate_direction_audit_before_job(
                    direction_audit_primary_path,
                    direction_audit_primary_sha256,
                    output_root=output_root,
                ),
                _validate_direction_audit_before_job(
                    direction_audit_confirmation_path,
                    direction_audit_confirmation_sha256,
                    output_root=output_root,
                ),
            ]
            if (
                direction_replay_sources[0]["path"]
                == direction_replay_sources[1]["path"]
                or direction_replay_sources[0]["sha256"]
                == direction_replay_sources[1]["sha256"]
            ):
                raise CoarseRepairAuditWorkerError(
                    "fixed-direction replay sources are not independent"
                )
        if continuation_requested:
            homotopy_audit_source = _validate_homotopy_audit_before_job(
                homotopy_audit_manifest_path,
                homotopy_audit_manifest_sha256,
                output_root=output_root,
            )
            if any(
                homotopy_audit_source["path"] == source["path"]
                or homotopy_audit_source["sha256"] == source["sha256"]
                for source in direction_replay_sources or []
            ):
                raise CoarseRepairAuditWorkerError(
                    "homotopy audit source is not independent of direction audits"
                )
        evidence_path = _evidence_path(output, output_root)
        wrapper = _wrapper_skeleton(
            config=config,
            output=output,
            evidence_path=evidence_path,
            characteristic=characteristic,
            limit_bytes=limit_bytes,
            config_sha256=config_sha256,
            contract_sha256=coarse_contract_sha256,
            projection_evidence=projection_evidence,
            local_schedule=local_schedule,
            direction_replay_sources=direction_replay_sources,
            argv=argv,
        )
        wrapper["direction_replay"]["homotopy_audit_source"] = (
            copy.deepcopy(homotopy_audit_source)
        )
        _write_new_json(evidence_path, wrapper)

        memory = memory_api or _base._default_memory_api()
        wrapper["worker_memory"]["system_before_limit"] = (
            _base._require_memory_evidence(
                memory.capture_system_physical_memory(),
                "system memory before repair audit limit",
            )
        )
        wrapper["worker_memory"]["process_before_limit"] = (
            _base._require_memory_evidence(
                memory.capture_current_process_memory(),
                "process memory before repair audit limit",
            )
        )
        wrapper["worker_memory"]["hard_limit"] = _base._require_memory_evidence(
            memory.install_current_process_memory_limit(limit_bytes),
            "repair audit worker hard memory limit",
            requested_limit_bytes=limit_bytes,
        )
        wrapper["worker"]["hard_limit_installed_before_heavy_import"] = True
        wrapper["worker_memory"]["system_after_limit"] = (
            _base._require_memory_evidence(
                memory.capture_system_physical_memory(),
                "system memory after repair audit limit",
            )
        )
        wrapper["worker_memory"]["process_after_limit"] = (
            _base._require_memory_evidence(
                memory.capture_current_process_memory(),
                "process memory after repair audit limit",
            )
        )
        _base._atomic_write_json(evidence_path, wrapper)

        restore_sigbreak = sigbreak_installer()
        heavy = heavy_loader()
        wrapper["worker"]["heavy_dependencies_imported"] = True
        contract = heavy.normalize_coarse_mesh_config(document, repository_root=root)
        if contract.get("normalized_config_sha256") != coarse_contract_sha256:
            raise CoarseRepairAuditWorkerError(
                "normalized coarse contract differs from controller preflight"
            )
        normalized_resource = contract.get("resource")
        if (
            not isinstance(normalized_resource, Mapping)
            or dict(normalized_resource) != raw_resource
            or normalized_resource.get("calibration_worker_memory_limit_bytes")
            != limit_bytes
        ):
            raise CoarseRepairAuditWorkerError(
                "normalized repair-audit resource policy differs from raw TOML"
            )
        wrapper["worker"]["normalized_resource_matches_raw"] = True

        binding_arguments = {
            "coarse_contract": contract,
            "plan_path": local_schedule["path"],
            "plan_sha256": local_schedule["sha256"],
        }
        if "expected_coarse_contract_sha256" in inspect.signature(
            heavy.load_and_bind_local_schedule
        ).parameters:
            binding_arguments["expected_coarse_contract_sha256"] = str(
                projection_evidence["coarse_contract_sha256"]
            )
        binding = heavy.load_and_bind_local_schedule(**binding_arguments)
        if (
            not isinstance(binding, Mapping)
            or binding.get("schema")
            != "cfdpipe.boundary_layer_local_schedule_binding.v1"
            or binding.get("status") != "PASS"
            or binding.get("source_plan_path") != local_schedule["path"]
            or binding.get("source_plan_sha256") != local_schedule["sha256"]
            or binding.get("source_plan_size_bytes") != local_schedule["size_bytes"]
            or not _is_sha256(binding.get("binding_sha256"))
        ):
            raise CoarseRepairAuditWorkerError(
                "repair audit local schedule binding is stale"
            )
        wrapper["worker"]["local_schedule_bound_after_heavy_import"] = True
        wrapper["local_schedule"]["binding"] = {
            key: binding.get(key)
            for key in (
                "schema",
                "status",
                "source_plan_path",
                "source_plan_sha256",
                "source_plan_size_bytes",
                "coarse_contract_sha256",
                "surface_count",
                "layer_count",
                "binding_sha256",
            )
        }

        direction_replay_approval = None
        if replay_requested:
            audit_module = importlib.import_module(
                "cfdpipe.coarse_repair_audit"
            )
            replay_module = importlib.import_module(
                "cfdpipe.coarse_direction_replay"
            )
            expected_audit_strategy = (
                audit_module.make_coarse_repair_audit_strategy_config(
                    contract,
                    characteristic,
                    local_schedule_binding=binding,
                    schedule_feasibility_endpoint=(
                        "minimum-growth-owner-free-component-pattern-audit"
                    ),
                )
            )
            quality = contract.get("quality")
            if not isinstance(quality, Mapping):
                raise CoarseRepairAuditWorkerError(
                    "coarse contract quality policy is missing"
                )
            direction_replay_approval = (
                replay_module.load_direction_replay_consensus_approval(
                    direction_audit_primary_path,
                    direction_audit_primary_sha256,
                    direction_audit_confirmation_path,
                    direction_audit_confirmation_sha256,
                    expected_audit_strategy=expected_audit_strategy,
                    expected_coarse_contract_sha256=str(
                        projection_evidence["coarse_contract_sha256"]
                    ),
                    expected_characteristic_length_m=characteristic,
                    expected_local_schedule_binding=binding,
                    expected_projection_evidence=projection_evidence,
                    expected_projected_3d_elements=int(
                        projection_evidence["projected_3d_elements"]
                    ),
                    expected_worker_memory_limit_bytes=int(
                        contract["resource"][
                            "calibration_worker_memory_limit_bytes"
                        ]
                    ),
                    expected_minimum_prism_scaled_jacobian=float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    expected_minimum_core_tetra_gamma=float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                )
            )
            if (
                direction_replay_approval.get("approval_sha256")
                != direction_replay_approval_sha256
            ):
                raise CoarseRepairAuditWorkerError(
                    "worker direction replay approval differs from the controller"
                )
            wrapper["direction_replay"]["approval"] = copy.deepcopy(
                direction_replay_approval
            )
            if combined_replay_requested:
                try:
                    design = contract["boundary_layer_design"]
                    homotopy_endpoint = (
                        replay_module.make_physical_schedule_homotopy_endpoint(
                            baseline_binding_sha256=str(
                                direction_replay_approval.get(
                                    "local_schedule_binding_sha256",
                                    binding["binding_sha256"],
                                )
                            ),
                            first_layer_height_m=float(
                                design["first_layer_height_m"]
                            ),
                            layer_count=int(design["layer_count"]),
                        )
                    )
                    homotopy_endpoint = (
                        replay_module.validate_physical_schedule_homotopy_endpoint(
                            homotopy_endpoint,
                            expected_binding_sha256=str(
                                direction_replay_approval.get(
                                    "local_schedule_binding_sha256",
                                    binding["binding_sha256"],
                                )
                            ),
                            expected_first_layer_height_m=float(
                                design["first_layer_height_m"]
                            ),
                            expected_layer_count=int(design["layer_count"]),
                        )
                    )
                except Exception as error:
                    raise CoarseRepairAuditWorkerError(
                        f"worker physical-schedule homotopy endpoint is invalid: {error}"
                    ) from error
                if (
                    homotopy_endpoint.get("endpoint_sha256")
                    != physical_schedule_homotopy_endpoint_sha256
                ):
                    raise CoarseRepairAuditWorkerError(
                        "worker homotopy endpoint differs from the controller"
                    )
                wrapper["direction_replay"]["homotopy_endpoint"] = (
                    copy.deepcopy(homotopy_endpoint)
                )
            if continuation_requested:
                continuation_module = importlib.import_module(
                    "cfdpipe.coarse_schedule_continuation"
                )
                continuation_discovery_schema = getattr(
                    continuation_module, "DISCOVERY_SCHEMA", None
                )
                if (
                    not isinstance(continuation_discovery_schema, str)
                    or not continuation_discovery_schema
                ):
                    raise CoarseRepairAuditWorkerError(
                        "worker schedule direction continuation discovery schema "
                        "is unavailable"
                    )
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
                    audit_module._physical_surface_schedule_values(
                        binding,
                        expected_surface_count=expected_surface_count,
                        expected_layer_count=expected_layer_count,
                    )
                )
                homotopy_audit_evidence = (
                    audit_module.load_and_validate_physical_homotopy_audit_manifest(
                        homotopy_audit_manifest_path,
                        homotopy_audit_manifest_sha256,
                        output_root=output_root,
                        expected_contract_sha256=str(
                            projection_evidence["coarse_contract_sha256"]
                        ),
                        expected_characteristic_length_m=characteristic,
                        expected_projection_evidence=projection_evidence,
                        expected_local_schedule_binding=binding,
                        expected_approval=direction_replay_approval,
                        expected_endpoint=homotopy_endpoint,
                        expected_projected_3d_elements=int(
                            projection_evidence["projected_3d_elements"]
                        ),
                        expected_prism_element_count=int(
                            source_quality["prism_element_count"]
                        ),
                        expected_core_element_count=int(
                            source_quality["core_element_count"]
                        ),
                        expected_layer_count=expected_layer_count,
                        expected_surface_count=expected_surface_count,
                        expected_surface_schedules=expected_surface_schedules,
                        expected_minimum_prism_scaled_jacobian=float(
                            quality["minimum_prism_scaled_jacobian"]
                        ),
                        expected_minimum_core_tetra_gamma=float(
                            quality["minimum_core_tetra_gamma"]
                        ),
                    )
                )
                endpoint_arguments = {
                    "homotopy_manifest_sha256": (
                        homotopy_audit_manifest_sha256
                    ),
                    "homotopy_discovery": homotopy_audit_evidence[
                        "discovery"
                    ],
                    "homotopy_endpoint_sha256": homotopy_audit_evidence[
                        "discovery"
                    ].get("source_bindings", {}).get(
                        "homotopy_endpoint_sha256",
                        homotopy_endpoint["endpoint_sha256"],
                    ),
                    "direction_replay_approval_sha256": (
                        homotopy_audit_evidence["discovery"].get(
                            "source_bindings", {}
                        ).get(
                            "direction_replay_approval_sha256",
                            direction_replay_approval_sha256,
                        )
                    ),
                    "local_schedule_binding_sha256": homotopy_audit_evidence[
                        "discovery"
                    ].get("source_bindings", {}).get(
                        "local_schedule_binding_sha256",
                        binding["binding_sha256"],
                    ),
                    "minimum_prism_scaled_jacobian_for_pass": float(
                        quality["minimum_prism_scaled_jacobian"]
                    ),
                    "minimum_core_tetra_gamma_for_pass": float(
                        quality["minimum_core_tetra_gamma"]
                    ),
                    "layer_count": expected_layer_count,
                    "first_layer_height_m": float(
                        contract["boundary_layer_design"][
                            "first_layer_height_m"
                        ]
                    ),
                }
                try:
                    continuation_endpoint = (
                        continuation_module.make_schedule_frontier_direction_endpoint(
                            **endpoint_arguments
                        )
                    )
                    continuation_endpoint = (
                        continuation_module.validate_schedule_frontier_direction_endpoint(
                            continuation_endpoint,
                            **endpoint_arguments,
                        )
                    )
                except Exception as error:
                    raise CoarseRepairAuditWorkerError(
                        "worker schedule direction continuation endpoint is invalid: "
                        f"{error}"
                    ) from error
                if (
                    continuation_endpoint.get("endpoint_sha256")
                    != schedule_direction_continuation_endpoint_sha256
                ):
                    raise CoarseRepairAuditWorkerError(
                        "worker schedule direction continuation endpoint differs "
                        "from the controller"
                    )
                wrapper["direction_replay"]["homotopy_audit_source"] = {
                    key: homotopy_audit_evidence[key]
                    for key in (
                        "path",
                        "sha256",
                        "size_bytes",
                        "manifest_status",
                    )
                }
                wrapper["direction_replay"][
                    "schedule_direction_continuation_endpoint"
                ] = copy.deepcopy(continuation_endpoint)

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
        wrapper["worker"]["audit_runner_called"] = True
        _base._atomic_write_json(evidence_path, wrapper)
        run_evidence: dict[str, Any] = {
            "worker_argv": list(argv),
            "config_path": str(config),
            "config_sha256": config_sha256,
            "coarse_contract_sha256": coarse_contract_sha256,
        }
        audit_arguments: dict[str, Any] = {
            "contract": contract,
            "characteristic_length_m": characteristic,
            "strategy": strategy,
            "local_schedule_binding": binding,
            "projection_evidence": projection_evidence,
            "run_evidence": run_evidence,
        }
        if schedule_feasibility_endpoint is not None:
            run_evidence["schedule_feasibility_endpoint"] = (
                schedule_feasibility_endpoint
            )
            audit_arguments["schedule_feasibility_endpoint"] = (
                schedule_feasibility_endpoint
            )
        if direction_replay_approval is not None:
            run_evidence.update(
                {
                    "direction_audit_primary_path": str(
                        direction_audit_primary_path.resolve(strict=True)
                    ),
                    "direction_audit_primary_sha256": (
                        direction_audit_primary_sha256
                    ),
                    "direction_audit_confirmation_path": str(
                        direction_audit_confirmation_path.resolve(strict=True)
                    ),
                    "direction_audit_confirmation_sha256": (
                        direction_audit_confirmation_sha256
                    ),
                    "direction_replay_approval_sha256": (
                        direction_replay_approval_sha256
                    ),
                }
            )
            if combined_replay_requested:
                run_evidence[
                    "physical_schedule_homotopy_endpoint_sha256"
                ] = physical_schedule_homotopy_endpoint_sha256
            if continuation_requested:
                run_evidence.update(
                    {
                        "homotopy_audit_manifest_path": str(
                            homotopy_audit_manifest_path.resolve(strict=True)
                        ),
                        "homotopy_audit_manifest_sha256": (
                            homotopy_audit_manifest_sha256
                        ),
                        "schedule_direction_continuation_endpoint_sha256": (
                            schedule_direction_continuation_endpoint_sha256
                        ),
                    }
                )
                audit_arguments["homotopy_audit_evidence"] = (
                    homotopy_audit_evidence
                )
            audit_arguments["direction_replay_approval"] = (
                direction_replay_approval
            )
        manifest = heavy.audit_runner(
            **audit_arguments,
        )
        if not isinstance(manifest, dict):
            raise CoarseRepairAuditWorkerError(
                "repair audit runner did not return a manifest"
            )
        wrapper["worker_memory"]["process_after_audit"] = (
            _base._require_memory_evidence(
                memory.capture_current_process_memory(),
                "process memory after repair audit",
            )
        )
        wrapper["worker_memory"]["system_after_audit"] = (
            _base._require_memory_evidence(
                memory.capture_system_physical_memory(),
                "system memory after repair audit",
            )
        )
        wrapper["audit_result_status"] = manifest.get("status")
        expected_discovery_schema = (
            (
                continuation_discovery_schema
                if continuation_requested
                else (
                    "cfdpipe.coarse_physical_schedule_fixed_direction_homotopy_discovery.v1"
                    if homotopy_requested
                    else "cfdpipe.coarse_physical_schedule_fixed_direction_discovery.v1"
                )
            )
            if direction_replay_approval is not None
            else (
                "cfdpipe.coarse_component_direction_discovery.v1"
                if schedule_feasibility_endpoint
                in (
                    "minimum-growth-owner-free-component-direction-audit",
                    "minimum-growth-owner-free-component-pattern-audit",
                )
                else "cfdpipe.coarse_repair_discovery.v1"
            )
        )
        expected_purpose = (
            (
                SCHEDULE_CONTINUATION_PURPOSE
                if continuation_requested
                else (
                    PHYSICAL_HOMOTOPY_PURPOSE
                    if homotopy_requested
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
        )
        complete_pass = (
            manifest.get("schema") == "cfdpipe.coarse_repair_audit_manifest.v1"
            and manifest.get("status") == "PASS"
            and manifest.get("audit_only") is True
            and manifest.get("calibration_PASS_authorized") is False
            and manifest.get("production_mesh_eligible") is False
            and manifest.get("su2_called") is False
            and manifest.get("paraview_called") is False
            and manifest.get("external_commands") == []
            and isinstance(manifest.get("repair_discovery"), Mapping)
            and manifest["repair_discovery"].get("schema")
            == expected_discovery_schema
            and manifest["repair_discovery"].get("status") == "PASS"
            and manifest["repair_discovery"].get("profile_complete") is True
            and manifest.get("audit_purpose") == expected_purpose
            and manifest.get("schedule_feasibility_endpoint_requested")
            == schedule_feasibility_endpoint
            and manifest.get("direction_replay_approval")
            == direction_replay_approval
            and manifest.get("physical_schedule_homotopy_endpoint")
            == (
                homotopy_endpoint
                if combined_replay_requested
                else None
            )
            and manifest.get("schedule_direction_continuation_endpoint")
            == (
                continuation_endpoint
                if continuation_requested
                else None
            )
            and manifest.get("calibration_PASS_authorized") is False
            and manifest.get("production_mesh_eligible") is False
            and manifest.get("mesh_written") is False
        )
        structured_physical_incomplete = (
            direction_replay_approval is not None
            and manifest.get("schema")
            == "cfdpipe.coarse_repair_audit_manifest.v1"
            and manifest.get("status") == "INCOMPLETE"
            and manifest.get("audit_only") is True
            and manifest.get("calibration_PASS_authorized") is False
            and manifest.get("production_mesh_eligible") is False
            and manifest.get("mesh_written") is False
            and manifest.get("su2_called") is False
            and manifest.get("paraview_called") is False
            and manifest.get("external_commands") == []
            and manifest.get("error") is None
            and isinstance(manifest.get("repair_discovery"), Mapping)
            and manifest["repair_discovery"].get("schema")
            == expected_discovery_schema
            and manifest["repair_discovery"].get("status") == "INCOMPLETE"
            and manifest["repair_discovery"].get("profile_complete") is False
            and manifest.get("audit_purpose") == expected_purpose
            and manifest.get("schedule_feasibility_endpoint_requested")
            == schedule_feasibility_endpoint
            and manifest.get("direction_replay_approval")
            == direction_replay_approval
            and manifest.get("physical_schedule_homotopy_endpoint")
            == (
                homotopy_endpoint
                if combined_replay_requested
                else None
            )
            and manifest.get("schedule_direction_continuation_endpoint")
            == (
                continuation_endpoint
                if continuation_requested
                else None
            )
        )
        wrapper["status"] = (
            "PASS" if complete_pass or structured_physical_incomplete else "FAIL"
        )
        wrapper["ended_at_utc"] = _utc_now()
        manifest["worker_isolation"] = copy.deepcopy(wrapper)
        output.mkdir(parents=True, exist_ok=False)
        _write_new_json(output / MANIFEST_NAME, manifest)
        _base._atomic_write_json(evidence_path, wrapper)
        return (
            0 if complete_pass else (2 if structured_physical_incomplete else 1)
        ), manifest
    except BaseException as error:
        restore_error: BaseException | None = None
        if restore_sigbreak is not None:
            try:
                restore_sigbreak()
            except BaseException as caught_restore_error:
                restore_error = caught_restore_error
                traceback.print_exception(
                    type(caught_restore_error),
                    caught_restore_error,
                    caught_restore_error.__traceback__,
                )
            finally:
                restore_sigbreak = None
        traceback.print_exception(type(error), error, error.__traceback__)
        if wrapper is not None and evidence_path is not None:
            wrapper["status"] = "FAIL"
            wrapper["ended_at_utc"] = _utc_now()
            wrapper["worker_error"] = _error_record(error)
            if restore_error is not None:
                wrapper["worker_error"]["sigbreak_restore_error"] = (
                    _error_record(restore_error)
                )
            if memory is not None and wrapper["worker"][
                "hard_limit_installed_before_heavy_import"
            ]:
                try:
                    wrapper["worker_memory"]["process_after_audit"] = (
                        _base._require_memory_evidence(
                            memory.capture_current_process_memory(),
                            "process memory after failed repair audit",
                        )
                    )
                    wrapper["worker_memory"]["system_after_audit"] = (
                        _base._require_memory_evidence(
                            memory.capture_system_physical_memory(),
                            "system memory after failed repair audit",
                        )
                    )
                except BaseException as memory_error:
                    wrapper["worker_error"]["post_failure_memory_error"] = (
                        _error_record(memory_error)
                    )
            try:
                _base._atomic_write_json(evidence_path, wrapper)
            except BaseException as write_error:
                traceback.print_exception(
                    type(write_error), write_error, write_error.__traceback__
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
    finally:
        if restore_sigbreak is not None:
            restore_sigbreak()


def _compact_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    discovery = manifest.get("repair_discovery")
    return {
        "status": manifest.get("status", "FAIL"),
        "profile_complete": (
            discovery.get("profile_complete")
            if isinstance(discovery, Mapping)
            else False
        ),
        "manifest": (
            manifest.get("worker_isolation", {})
            .get("worker", {})
            .get("manifest_path")
            if isinstance(manifest.get("worker_isolation"), Mapping)
            else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    worker_argv = [
        str(Path(sys.executable).resolve()),
        "-m",
        "cfdpipe.coarse_repair_audit_worker",
        *raw_argv,
    ]
    code, manifest = execute_repair_audit_worker(
        config_path=args.config,
        config_sha256=args.config_sha256,
        coarse_contract_sha256=args.coarse_contract_sha256,
        characteristic_length_m=args.characteristic_length,
        projection_manifest_path=args.projection_manifest,
        projection_manifest_sha256=args.projection_sha256,
        local_schedule_path=args.local_schedule,
        local_schedule_sha256=args.local_schedule_sha256,
        output_directory=args.output,
        argv=worker_argv,
        schedule_feasibility_endpoint=args.schedule_feasibility_endpoint,
        direction_audit_primary_path=args.direction_audit_primary,
        direction_audit_primary_sha256=(
            args.direction_audit_primary_sha256
        ),
        direction_audit_confirmation_path=args.direction_audit_confirmation,
        direction_audit_confirmation_sha256=(
            args.direction_audit_confirmation_sha256
        ),
        direction_replay_approval_sha256=(
            args.direction_replay_approval_sha256
        ),
        physical_schedule_homotopy_endpoint_sha256=(
            args.physical_schedule_homotopy_endpoint_sha256
        ),
        homotopy_audit_manifest_path=args.homotopy_audit_manifest,
        homotopy_audit_manifest_sha256=(
            args.homotopy_audit_manifest_sha256
        ),
        schedule_direction_continuation_endpoint_sha256=(
            args.schedule_direction_continuation_endpoint_sha256
        ),
    )
    print(json.dumps(_compact_summary(manifest), ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CoarseRepairAuditWorkerError",
    "MANIFEST_NAME",
    "WORKER_EVIDENCE_NAME",
    "WORKER_EVIDENCE_SCHEMA",
    "execute_repair_audit_worker",
]
