"""Fail-closed resource preflights for isolated coarse-mesh calibrations.

This module is deliberately side-effect free except for the optional system
snapshot helper.  It never imports Gmsh and never starts an external process.
Callers supply the already-normalized coarse-mesh contract and may inject a
snapshot in tests or capture one immediately before launching a worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
import math
import os
from pathlib import Path
import shutil
from typing import Any

from .resource_gate import (
    MINIMUM_CALIBRATION_SAMPLES,
    OS_RESERVE_BYTES,
    SAFETY_FACTOR,
)


REPORT_SCHEMA = "cfdpipe.coarse_calibration_preflight.v2"
CALIBRATION_MANIFEST_SCHEMA = "cfdpipe.coarse_mesh_calibration_manifest.v1"
PROCESS_MEMORY_REPORT_SCHEMA = "cfdpipe.worker_process_memory.v2"
PRIVATE_COMMIT_SEMANTICS_SCHEMA = "cfdpipe.windows_private_commit_semantics.v1"
MINIMUM_DISK_FREE_BYTES = 20 * 1024**3
ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS = 840.0

_PRIVATE_COMMIT_SEMANTICS = {
    "schema": PRIVATE_COMMIT_SEMANTICS_SCHEMA,
    "job_object_process_memory_limit_basis": "private_commit_bytes",
    "current_private_commit_metric": "metrics.private_usage_bytes",
    "current_private_commit_source": (
        "PROCESS_MEMORY_COUNTERS_EX.PrivateUsage"
    ),
    "peak_private_commit_metric": "metrics.peak_pagefile_usage_bytes",
    "peak_private_commit_source": (
        "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
    ),
    "pagefile_field_means_commit_charge_not_pagefile_residency": True,
    "working_set_is_not_job_process_memory_limit_metric": True,
}

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


class CoarseCalibrationGateError(RuntimeError):
    """Raised only when a local system snapshot cannot be collected."""


def _safe_scalar(value: Any) -> int | float | str | bool | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return repr(value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _reason(
    report: dict[str, Any], code: str, message: str, *, field: str | None = None
) -> None:
    reason: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        reason["field"] = field
    report["reasons"].append(reason)


def _base_report(stage: str) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "FAIL",
        "stage": stage,
        "policy": {
            "os_physical_memory_reserve_bytes": OS_RESERVE_BYTES,
            "minimum_disk_free_bytes": MINIMUM_DISK_FREE_BYTES,
            "safety_factor": SAFETY_FACTOR,
            "absolute_maximum_timeout_seconds": ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS,
            "pagefile_can_rescue_physical_failure": False,
            "physical_memory_formula": (
                "os_physical_memory_reserve_bytes + "
                "calibration_worker_memory_limit_bytes + "
                "worker_monitor_margin_bytes"
            ),
            "second_point_scale_model": "(first_h/second_h)^3",
        },
        "inputs": {"contract": {}, "system_snapshot": {}},
        "projections": {},
        "requirements": {},
        "checks": {},
        "reasons": [],
    }


def _normalize_contract(
    report: dict[str, Any], contract: Any
) -> dict[str, Any] | None:
    if not isinstance(contract, Mapping):
        _reason(
            report,
            "CONTRACT_INVALID",
            "contract must be an already-normalized mapping",
            field="contract",
        )
        return None

    raw_sizes = contract.get("calibration_characteristic_lengths_m")
    sizes_valid = (
        isinstance(raw_sizes, Sequence)
        and not isinstance(raw_sizes, (str, bytes, bytearray))
        and len(raw_sizes) >= 2
        and all(_positive_finite(value) for value in raw_sizes)
    )
    sizes = [float(value) for value in raw_sizes] if sizes_valid else []
    timeout = contract.get("calibration_maximum_elapsed_seconds")
    element_cap = contract.get("calibration_maximum_3d_elements")
    resource = contract.get("resource")
    resource_valid = isinstance(resource, Mapping)
    configured_reserve = (
        resource.get("os_physical_memory_reserve_bytes")
        if resource_valid
        else None
    )
    configured_safety = resource.get("safety_factor") if resource_valid else None
    configured_disk = (
        resource.get("minimum_disk_free_bytes") if resource_valid else None
    )
    projection_worker_limit = (
        resource.get("projection_worker_memory_limit_bytes")
        if resource_valid
        else None
    )
    calibration_worker_limit = (
        resource.get("calibration_worker_memory_limit_bytes")
        if resource_valid
        else None
    )
    worker_monitor_margin = (
        resource.get("worker_monitor_margin_bytes")
        if resource_valid
        else None
    )

    validations = (
        (
            "contract_schema",
            contract.get("schema") == "cfdpipe.coarse_mesh.v1",
            "CONTRACT_SCHEMA",
            "contract schema is not the normalized coarse-mesh schema",
            "contract.schema",
        ),
        (
            "contract_status",
            contract.get("status") == "CONFIGURED",
            "CONTRACT_STATUS",
            "contract status must be CONFIGURED",
            "contract.status",
        ),
        (
            "calibration_sizes",
            sizes_valid and len(set(sizes)) == len(sizes),
            "CALIBRATION_SIZES_INVALID",
            "contract must contain at least two distinct positive calibration sizes",
            "contract.calibration_characteristic_lengths_m",
        ),
        (
            "calibration_element_cap",
            _positive_int(element_cap),
            "CALIBRATION_ELEMENT_CAP_INVALID",
            "calibration element cap must be a positive integer",
            "contract.calibration_maximum_3d_elements",
        ),
        (
            "contract_timeout",
            _positive_finite(timeout)
            and float(timeout) <= ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS,
            "CONTRACT_TIMEOUT_INVALID",
            "contract calibration timeout must be positive and no greater than 840 seconds",
            "contract.calibration_maximum_elapsed_seconds",
        ),
        (
            "resource_policy",
            resource_valid
            and set(resource) == _RESOURCE_FIELDS
            and resource.get("minimum_calibration_samples")
            == MINIMUM_CALIBRATION_SAMPLES
            and configured_reserve == OS_RESERVE_BYTES
            and configured_safety == SAFETY_FACTOR
            and _positive_int(configured_disk)
            and int(configured_disk) >= MINIMUM_DISK_FREE_BYTES
            and resource.get("physical_memory_required") is True
            and resource.get("pagefile_cannot_rescue_physical_memory_failure") is True
            and _positive_int(projection_worker_limit)
            and _positive_int(calibration_worker_limit)
            and int(projection_worker_limit) <= int(calibration_worker_limit)
            and _positive_int(worker_monitor_margin),
            "RESOURCE_POLICY_INVALID",
            "normalized contract resource policy is incomplete or weakened",
            "contract.resource",
        ),
    )
    for check, passed, code, message, field in validations:
        report["checks"][check] = bool(passed)
        if not passed:
            _reason(report, code, message, field=field)

    report["inputs"]["contract"] = {
        "schema": _safe_scalar(contract.get("schema")),
        "status": _safe_scalar(contract.get("status")),
        "normalized_config_sha256": _safe_scalar(
            contract.get("normalized_config_sha256")
        ),
        "calibration_characteristic_lengths_m": sizes,
        "calibration_maximum_3d_elements": (
            int(element_cap) if _positive_int(element_cap) else None
        ),
        "calibration_maximum_elapsed_seconds": (
            float(timeout) if _positive_finite(timeout) else None
        ),
        "minimum_disk_free_bytes": (
            int(configured_disk) if _positive_int(configured_disk) else None
        ),
        "projection_worker_memory_limit_bytes": (
            int(projection_worker_limit)
            if _positive_int(projection_worker_limit)
            else None
        ),
        "calibration_worker_memory_limit_bytes": (
            int(calibration_worker_limit)
            if _positive_int(calibration_worker_limit)
            else None
        ),
        "worker_monitor_margin_bytes": (
            int(worker_monitor_margin)
            if _positive_int(worker_monitor_margin)
            else None
        ),
    }
    if any(not item[1] for item in validations):
        return None
    return {
        "sizes": sizes,
        "element_cap": int(element_cap),
        "maximum_timeout_seconds": float(timeout),
        "minimum_disk_free_bytes": max(
            MINIMUM_DISK_FREE_BYTES, int(configured_disk)
        ),
        "os_physical_memory_reserve_bytes": int(configured_reserve),
        "safety_factor": float(configured_safety),
        "projection_worker_memory_limit_bytes": int(projection_worker_limit),
        "calibration_worker_memory_limit_bytes": int(calibration_worker_limit),
        "worker_monitor_margin_bytes": int(worker_monitor_margin),
    }


def _normalize_snapshot(
    report: dict[str, Any], snapshot: Any
) -> dict[str, int] | None:
    if not isinstance(snapshot, Mapping):
        _reason(
            report,
            "SYSTEM_SNAPSHOT_INVALID",
            "system snapshot must be a mapping",
            field="system_snapshot",
        )
        return None
    normalized: dict[str, int | None] = {}
    valid = True
    for field in (
        "physical_total_bytes",
        "physical_available_bytes",
        "virtual_available_bytes",
        "disk_free_bytes",
    ):
        value = snapshot.get(field)
        field_valid = _nonnegative_int(value)
        report["checks"][f"{field}_input"] = field_valid
        normalized[field] = int(value) if field_valid else None
        if not field_valid:
            valid = False
            _reason(
                report,
                "SYSTEM_SNAPSHOT_VALUE_INVALID",
                f"{field} must be a nonnegative integer",
                field=f"system_snapshot.{field}",
            )
    report["inputs"]["system_snapshot"] = normalized
    if valid and (
        int(normalized["physical_total_bytes"])
        < int(normalized["physical_available_bytes"])
    ):
        valid = False
        _reason(
            report,
            "SYSTEM_SNAPSHOT_INCONSISTENT",
            "available physical memory cannot exceed total physical memory",
            field="system_snapshot.physical_available_bytes",
        )
    if not valid:
        return None
    return {field: int(value) for field, value in normalized.items()}


def _normalize_timeout(
    report: dict[str, Any], requested: Any, contract_maximum: float
) -> float | None:
    valid = _positive_finite(requested)
    report["checks"]["requested_timeout_input"] = valid
    report["inputs"]["requested_timeout_seconds"] = (
        float(requested) if valid else _safe_scalar(requested)
    )
    if not valid:
        _reason(
            report,
            "REQUESTED_TIMEOUT_INVALID",
            "requested timeout must be positive finite seconds",
            field="timeout_seconds",
        )
        return None
    timeout = float(requested)
    ceiling = min(contract_maximum, ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS)
    passed = timeout <= ceiling
    report["checks"]["requested_timeout_within_contract"] = passed
    if not passed:
        _reason(
            report,
            "REQUESTED_TIMEOUT_EXCEEDS_CONTRACT",
            f"requested timeout {timeout} exceeds contract ceiling {ceiling}",
            field="timeout_seconds",
        )
    return timeout if passed else None


def _add_pagefile_reason(
    report: dict[str, Any], snapshot: Mapping[str, int], *, virtual_requirement: int
) -> None:
    if snapshot["virtual_available_bytes"] >= virtual_requirement:
        _reason(
            report,
            "PAGEFILE_NOT_ACCEPTED",
            "virtual memory is sufficient but cannot rescue a failed physical-memory gate",
            field="system_snapshot.virtual_available_bytes",
        )


def evaluate_first_point_preflight(
    contract: Mapping[str, Any],
    system_snapshot: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Evaluate hard preconditions before the first calibration worker starts."""

    report = _base_report("FIRST_POINT")
    policy = _normalize_contract(report, contract)
    snapshot = _normalize_snapshot(report, system_snapshot)
    timeout = (
        _normalize_timeout(report, timeout_seconds, policy["maximum_timeout_seconds"])
        if policy is not None
        else None
    )
    required_physical = (
        policy["os_physical_memory_reserve_bytes"]
        + policy["calibration_worker_memory_limit_bytes"]
        + policy["worker_monitor_margin_bytes"]
        if policy is not None
        else None
    )
    minimum_disk = (
        policy["minimum_disk_free_bytes"]
        if policy is not None
        else MINIMUM_DISK_FREE_BYTES
    )
    report["requirements"] = {
        "physical_memory_formula": (
            "os_physical_memory_reserve_bytes + "
            "calibration_worker_memory_limit_bytes + "
            "worker_monitor_margin_bytes"
        ),
        "physical_total_bytes": required_physical,
        "physical_available_bytes": required_physical,
        "calibration_worker_memory_limit_bytes": (
            policy["calibration_worker_memory_limit_bytes"]
            if policy is not None
            else None
        ),
        "disk_free_bytes": minimum_disk,
        "pagefile_can_rescue_physical_failure": False,
        "timeout_seconds": (
            min(policy["maximum_timeout_seconds"], ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS)
            if policy is not None
            else None
        ),
    }

    if snapshot is not None and required_physical is not None:
        total_pass = snapshot["physical_total_bytes"] >= required_physical
        available_pass = snapshot["physical_available_bytes"] >= required_physical
        disk_pass = snapshot["disk_free_bytes"] >= minimum_disk
        report["checks"].update(
            {
                "physical_total_reserve": total_pass,
                "physical_available_minimum": available_pass,
                "disk_free_minimum": disk_pass,
            }
        )
        if not total_pass:
            _reason(
                report,
                "PHYSICAL_TOTAL_INSUFFICIENT",
                f"physical total must be at least {required_physical} bytes",
                field="system_snapshot.physical_total_bytes",
            )
        if not available_pass:
            _reason(
                report,
                "PHYSICAL_AVAILABLE_INSUFFICIENT",
                (
                    "first calibration requires at least "
                    f"{required_physical} available physical bytes for the OS "
                    "reserve, worker hard limit, and monitor margin"
                ),
                field="system_snapshot.physical_available_bytes",
            )
        if not disk_pass:
            _reason(
                report,
                "DISK_FREE_INSUFFICIENT",
                f"calibration requires at least {minimum_disk} free disk bytes",
                field="system_snapshot.disk_free_bytes",
            )
        if not total_pass or not available_pass:
            _add_pagefile_reason(
                report,
                snapshot,
                virtual_requirement=required_physical,
            )

    report["status"] = (
        "PASS"
        if policy is not None
        and snapshot is not None
        and timeout is not None
        and not report["reasons"]
        else "FAIL"
    )
    return report


def _sample_value(sample: Mapping[str, Any], field: str) -> Any:
    direct = sample.get(field)
    if direct is not None:
        return direct
    if field == "elements":
        generation = sample.get("generation_audit")
        if isinstance(generation, Mapping):
            return generation.get("element_count_3d")
    if field == "output_bytes":
        outputs = sample.get("outputs")
        if isinstance(outputs, Mapping) and outputs:
            sizes: list[int] = []
            for record in outputs.values():
                if not isinstance(record, Mapping) or not _positive_int(
                    record.get("size_bytes")
                ):
                    return None
                sizes.append(int(record["size_bytes"]))
            return sum(sizes)
    return None


def _normalize_private_commit_evidence(
    report: dict[str, Any], sample: Mapping[str, Any]
) -> dict[str, int] | None:
    """Validate the first worker's peak metric against Job Object semantics.

    ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` is a private-commit ceiling.  A peak
    Working Set is therefore deliberately ignored, even when present at the
    top level of an older calibration manifest.  Only the post-calibration
    PROCESS_MEMORY_COUNTERS_EX report embedded by the isolated worker is an
    admissible second-point projection seed.
    """

    source = (
        "worker_isolation.worker_memory.process_after_calibration."
        "metrics.peak_pagefile_usage_bytes"
    )
    audit: dict[str, Any] = {
        "source_path": source,
        "source_win32_field": (
            "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
        ),
        "metric_semantics": "peak_private_commit_bytes",
        "working_set_rejected_as_limit_metric": True,
        "status": "FAIL",
    }

    isolation = sample.get("worker_isolation")
    worker = isolation.get("worker") if isinstance(isolation, Mapping) else None
    worker_memory = (
        isolation.get("worker_memory")
        if isinstance(isolation, Mapping)
        else None
    )
    hard_limit = (
        worker_memory.get("hard_limit")
        if isinstance(worker_memory, Mapping)
        else None
    )
    process_report = (
        worker_memory.get("process_after_calibration")
        if isinstance(worker_memory, Mapping)
        else None
    )
    metrics = (
        process_report.get("metrics")
        if isinstance(process_report, Mapping)
        else None
    )
    semantics = (
        process_report.get("private_commit_semantics")
        if isinstance(process_report, Mapping)
        else None
    )
    job = hard_limit.get("job_object") if isinstance(hard_limit, Mapping) else None

    requested_limit = (
        hard_limit.get("requested_process_memory_limit_bytes")
        if isinstance(hard_limit, Mapping)
        else None
    )
    installed_limit = (
        job.get("process_memory_limit_bytes") if isinstance(job, Mapping) else None
    )
    current_private = (
        metrics.get("private_usage_bytes") if isinstance(metrics, Mapping) else None
    )
    current_pagefile = (
        metrics.get("pagefile_usage_bytes") if isinstance(metrics, Mapping) else None
    )
    peak_private = (
        metrics.get("peak_pagefile_usage_bytes")
        if isinstance(metrics, Mapping)
        else None
    )

    checks = {
        "worker_isolation_pass": (
            isinstance(isolation, Mapping) and isolation.get("status") == "PASS"
        ),
        "hard_limit_precedes_heavy_import": (
            isinstance(worker, Mapping)
            and worker.get("hard_limit_installed_before_heavy_import") is True
        ),
        "builder_called": (
            isinstance(worker, Mapping) and worker.get("builder_called") is True
        ),
        "hard_limit_evidence_pass": (
            isinstance(hard_limit, Mapping) and hard_limit.get("status") == "PASS"
        ),
        "hard_limit_exact_and_retained": (
            _positive_int(requested_limit)
            and installed_limit == requested_limit
            and isinstance(job, Mapping)
            and job.get("process_assigned") is True
            and job.get("handle_retained_for_process_lifetime") is True
        ),
        "process_report_schema": (
            isinstance(process_report, Mapping)
            and process_report.get("schema") == PROCESS_MEMORY_REPORT_SCHEMA
            and process_report.get("status") == "PASS"
        ),
        "private_commit_semantics": (
            isinstance(semantics, Mapping)
            and dict(semantics) == _PRIVATE_COMMIT_SEMANTICS
        ),
        "current_private_commit_positive": _positive_int(current_private),
        "current_commit_aliases_equal": (
            _positive_int(current_private)
            and current_pagefile == current_private
        ),
        "peak_private_commit_valid": (
            _positive_int(peak_private)
            and _positive_int(current_private)
            and peak_private >= current_private
        ),
        "peak_private_commit_within_installed_limit": (
            _positive_int(peak_private)
            and _positive_int(installed_limit)
            and peak_private <= installed_limit
        ),
    }
    audit["checks"] = checks
    audit["current_private_commit_bytes"] = (
        int(current_private) if _positive_int(current_private) else None
    )
    audit["peak_private_commit_bytes"] = (
        int(peak_private) if _positive_int(peak_private) else None
    )
    audit["installed_process_memory_limit_bytes"] = (
        int(installed_limit) if _positive_int(installed_limit) else None
    )
    valid = all(checks.values())
    audit["status"] = "PASS" if valid else "FAIL"
    report["inputs"]["first_sample_private_commit_evidence"] = audit
    report["checks"]["first_sample_private_commit_evidence"] = valid
    if not valid:
        failed = sorted(name for name, passed in checks.items() if not passed)
        _reason(
            report,
            "FIRST_SAMPLE_PRIVATE_COMMIT_EVIDENCE_INVALID",
            "first-point peak private-commit evidence is incomplete or invalid: "
            + ", ".join(failed),
            field=source,
        )
        return None
    return {
        "peak_private_commit_bytes": int(peak_private),
        "current_private_commit_bytes": int(current_private),
        "worker_memory_limit_bytes": int(installed_limit),
    }


def _normalize_first_sample(
    report: dict[str, Any], sample: Any
) -> dict[str, int | float] | None:
    if not isinstance(sample, Mapping):
        _reason(
            report,
            "FIRST_SAMPLE_INVALID",
            "first-point sample must be a mapping",
            field="first_sample",
        )
        return None
    private_commit = _normalize_private_commit_evidence(report, sample)
    status_pass = sample.get("status") == "PASS"
    schema = sample.get("schema")
    manifest_semantics_pass = True
    if schema is not None:
        manifest_semantics_pass = (
            schema == CALIBRATION_MANIFEST_SCHEMA
            and sample.get("calibration_only") is True
            and sample.get("production_mesh_eligible") is False
            and sample.get("su2_called") is False
            and sample.get("paraview_called") is False
            and sample.get("resource_sample_eligible") is True
        )
    raw = {
        "characteristic_length_m": _sample_value(
            sample, "characteristic_length_m"
        ),
        "elements": _sample_value(sample, "elements"),
        "peak_private_commit_bytes": (
            private_commit["peak_private_commit_bytes"]
            if private_commit is not None
            else None
        ),
        "worker_memory_limit_bytes": (
            private_commit["worker_memory_limit_bytes"]
            if private_commit is not None
            else None
        ),
        "elapsed_seconds": _sample_value(sample, "elapsed_seconds"),
        "output_bytes": _sample_value(sample, "output_bytes"),
    }
    validity = {
        "characteristic_length_m": _positive_finite(
            raw["characteristic_length_m"]
        ),
        "elements": _positive_int(raw["elements"]),
        "peak_private_commit_bytes": _positive_int(
            raw["peak_private_commit_bytes"]
        ),
        "worker_memory_limit_bytes": _positive_int(
            raw["worker_memory_limit_bytes"]
        ),
        "elapsed_seconds": _positive_finite(raw["elapsed_seconds"]),
        "output_bytes": _positive_int(raw["output_bytes"]),
    }
    normalized = {
        "status": _safe_scalar(sample.get("status")),
        "schema": _safe_scalar(schema),
        **{
            field: (
                float(value)
                if field in ("characteristic_length_m", "elapsed_seconds")
                and validity[field]
                else int(value)
                if validity[field]
                else None
            )
            for field, value in raw.items()
        },
    }
    report["inputs"]["first_sample"] = normalized
    report["checks"]["first_sample_status"] = status_pass
    report["checks"]["first_sample_manifest_semantics"] = manifest_semantics_pass
    report["checks"]["first_sample_values"] = all(validity.values())
    if not status_pass:
        _reason(
            report,
            "FIRST_SAMPLE_NOT_PASS",
            "second-point prediction requires a PASS first-point sample",
            field="first_sample.status",
        )
    if not manifest_semantics_pass:
        _reason(
            report,
            "FIRST_SAMPLE_MANIFEST_INVALID",
            "calibration manifest safety flags or schema are invalid",
            field="first_sample",
        )
    for field, passed in validity.items():
        if not passed:
            _reason(
                report,
                "FIRST_SAMPLE_VALUE_INVALID",
                f"first_sample.{field} must be a positive finite measurement",
                field=f"first_sample.{field}",
            )
    if (
        private_commit is None
        or not status_pass
        or not manifest_semantics_pass
        or not all(validity.values())
    ):
        return None
    return {
        "characteristic_length_m": float(raw["characteristic_length_m"]),
        "elements": int(raw["elements"]),
        "peak_private_commit_bytes": int(raw["peak_private_commit_bytes"]),
        "worker_memory_limit_bytes": int(raw["worker_memory_limit_bytes"]),
        "elapsed_seconds": float(raw["elapsed_seconds"]),
        "output_bytes": int(raw["output_bytes"]),
    }


def _configured_size(
    sizes: Sequence[float], requested: Any
) -> float | None:
    if not _positive_finite(requested):
        return None
    value = float(requested)
    return next(
        (
            configured
            for configured in sizes
            if math.isclose(value, configured, rel_tol=1.0e-12, abs_tol=0.0)
        ),
        None,
    )


def evaluate_second_point_preflight(
    contract: Mapping[str, Any],
    system_snapshot: Mapping[str, Any],
    first_sample: Mapping[str, Any],
    *,
    timeout_seconds: float,
    second_characteristic_length_m: float | None = None,
) -> dict[str, Any]:
    """Project and gate a second calibration point from the first PASS sample."""

    report = _base_report("SECOND_POINT")
    policy = _normalize_contract(report, contract)
    snapshot = _normalize_snapshot(report, system_snapshot)
    sample = _normalize_first_sample(report, first_sample)
    timeout = (
        _normalize_timeout(report, timeout_seconds, policy["maximum_timeout_seconds"])
        if policy is not None
        else None
    )

    second_size: float | None = None
    if policy is not None:
        default_second = policy["sizes"][1]
        if sample is not None:
            first_size = float(sample["characteristic_length_m"])
            for index, configured_size in enumerate(policy["sizes"][:-1]):
                if math.isclose(
                    first_size,
                    configured_size,
                    rel_tol=1.0e-12,
                    abs_tol=0.0,
                ):
                    default_second = policy["sizes"][index + 1]
                    break
        requested_size = (
            default_second
            if second_characteristic_length_m is None
            else second_characteristic_length_m
        )
        second_size = _configured_size(policy["sizes"], requested_size)
        report["inputs"]["second_characteristic_length_m"] = _safe_scalar(
            requested_size
        )
        configured = second_size is not None
        report["checks"]["second_size_configured"] = configured
        if not configured:
            _reason(
                report,
                "SECOND_SIZE_NOT_CONFIGURED",
                "second characteristic length is not authorized by the contract",
                field="second_characteristic_length_m",
            )

    if policy is not None and sample is not None:
        first_size = float(sample["characteristic_length_m"])
        first_configured = _configured_size(policy["sizes"], first_size) is not None
        report["checks"]["first_size_configured"] = first_configured
        if not first_configured:
            _reason(
                report,
                "FIRST_SIZE_NOT_CONFIGURED",
                "first sample characteristic length is not authorized by the contract",
                field="first_sample.characteristic_length_m",
            )
        memory_limit_matches = (
            int(sample["worker_memory_limit_bytes"])
            == policy["calibration_worker_memory_limit_bytes"]
        )
        report["checks"]["first_sample_worker_limit_matches_contract"] = (
            memory_limit_matches
        )
        if not memory_limit_matches:
            _reason(
                report,
                "FIRST_SAMPLE_WORKER_LIMIT_MISMATCH",
                "first sample was measured under a different process private-commit limit",
                field=(
                    "worker_isolation.worker_memory.hard_limit."
                    "job_object.process_memory_limit_bytes"
                ),
            )
        if second_size is not None:
            order_valid = second_size < first_size
            report["checks"]["second_size_is_finer"] = order_valid
            if not order_valid:
                _reason(
                    report,
                    "SECOND_SIZE_NOT_FINER",
                    "second characteristic length must be smaller than the first sample length",
                    field="second_characteristic_length_m",
                )

    can_project = (
        policy is not None
        and snapshot is not None
        and sample is not None
        and timeout is not None
        and second_size is not None
        and report["checks"].get("first_size_configured") is True
        and report["checks"].get("first_sample_worker_limit_matches_contract")
        is True
        and report["checks"].get("second_size_is_finer") is True
    )
    if can_project:
        first_size = float(sample["characteristic_length_m"])
        size_ratio = first_size / second_size
        scale = size_ratio**3
        values = (size_ratio, scale)
        if any(not math.isfinite(value) or value <= 1.0 for value in values):
            _reason(
                report,
                "SECOND_PROJECTION_INVALID",
                "second-point cubic scale is non-finite or non-increasing",
            )
            can_project = False

    if can_project:
        predicted_elements = math.ceil(int(sample["elements"]) * scale)
        projected_peak_private_commit = math.ceil(
            int(sample["peak_private_commit_bytes"]) * scale
        )
        safe_peak_private_commit = math.ceil(
            projected_peak_private_commit * policy["safety_factor"]
        )
        worker_hard_limit = policy["calibration_worker_memory_limit_bytes"]
        required_physical = (
            policy["os_physical_memory_reserve_bytes"]
            + worker_hard_limit
            + policy["worker_monitor_margin_bytes"]
        )
        projected_elapsed = float(sample["elapsed_seconds"]) * scale
        safe_elapsed = projected_elapsed * policy["safety_factor"]
        projected_output = math.ceil(int(sample["output_bytes"]) * scale)
        safe_output = math.ceil(projected_output * policy["safety_factor"])
        required_disk = max(policy["minimum_disk_free_bytes"], safe_output)
        report["projections"] = {
            "first_to_second_characteristic_length_ratio": size_ratio,
            "cubic_scale_factor": scale,
            "elements": predicted_elements,
            "peak_private_commit_bytes": projected_peak_private_commit,
            "peak_private_commit_with_safety_bytes": safe_peak_private_commit,
            "peak_private_commit_source": (
                "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
            ),
            "working_set_used_for_limit_projection": False,
            "elapsed_seconds": projected_elapsed,
            "elapsed_with_safety_seconds": safe_elapsed,
            "output_bytes": projected_output,
            "output_with_safety_bytes": safe_output,
        }
        report["requirements"] = {
            "physical_memory_formula": (
                "os_physical_memory_reserve_bytes + "
                "calibration_worker_memory_limit_bytes + "
                "worker_monitor_margin_bytes"
            ),
            "physical_total_bytes": required_physical,
            "physical_available_bytes": required_physical,
            "calibration_worker_memory_limit_bytes": worker_hard_limit,
            "predicted_peak_private_commit_with_safety_bytes": (
                safe_peak_private_commit
            ),
            "worker_limit_metric": "private_commit_bytes",
            "pagefile_can_rescue_physical_failure": False,
            "disk_free_bytes": required_disk,
            "elapsed_seconds": min(
                timeout,
                policy["maximum_timeout_seconds"],
                ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS,
            ),
            "maximum_3d_elements": policy["element_cap"],
        }
        checks = {
            "predicted_elements_within_cap": (
                predicted_elements <= policy["element_cap"]
            ),
            "predicted_peak_private_commit_within_worker_hard_limit": (
                safe_peak_private_commit <= worker_hard_limit
            ),
            "physical_total": (
                snapshot["physical_total_bytes"] >= required_physical
            ),
            "physical_available": (
                snapshot["physical_available_bytes"] >= required_physical
            ),
            "elapsed_budget": safe_elapsed
            <= min(
                timeout,
                policy["maximum_timeout_seconds"],
                ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS,
            ),
            "disk_free": snapshot["disk_free_bytes"] >= required_disk,
        }
        report["checks"].update(checks)
        failures = (
            (
                "predicted_elements_within_cap",
                "PREDICTED_ELEMENT_CAP_EXCEEDED",
                f"predicted second-point elements {predicted_elements} exceed cap {policy['element_cap']}",
                "first_sample.elements",
            ),
            (
                "predicted_peak_private_commit_within_worker_hard_limit",
                "PREDICTED_PEAK_EXCEEDS_WORKER_HARD_LIMIT",
                (
                    "predicted peak private commit with safety "
                    f"{safe_peak_private_commit} bytes exceeds the configured "
                    f"worker private-commit hard limit {worker_hard_limit} bytes"
                ),
                (
                    "worker_isolation.worker_memory.process_after_calibration."
                    "metrics.peak_pagefile_usage_bytes"
                ),
            ),
            (
                "physical_total",
                "PHYSICAL_TOTAL_INSUFFICIENT",
                f"physical total is below required {required_physical} bytes",
                "system_snapshot.physical_total_bytes",
            ),
            (
                "physical_available",
                "PHYSICAL_AVAILABLE_INSUFFICIENT",
                f"available physical memory is below required {required_physical} bytes",
                "system_snapshot.physical_available_bytes",
            ),
            (
                "elapsed_budget",
                "PREDICTED_TIMEOUT_EXCEEDED",
                f"predicted elapsed time with safety {safe_elapsed} exceeds the allowed timeout",
                "first_sample.elapsed_seconds",
            ),
            (
                "disk_free",
                "DISK_FREE_INSUFFICIENT",
                f"free disk is below required {required_disk} bytes",
                "system_snapshot.disk_free_bytes",
            ),
        )
        for key, code, message, field in failures:
            if not checks[key]:
                _reason(report, code, message, field=field)
        if not checks["physical_total"] or not checks["physical_available"]:
            _add_pagefile_reason(
                report, snapshot, virtual_requirement=required_physical
            )

    report["status"] = "PASS" if can_project and not report["reasons"] else "FAIL"
    return report


def evaluate_coarse_calibration_preflight(
    contract: Mapping[str, Any],
    system_snapshot: Mapping[str, Any],
    *,
    point_index: int,
    timeout_seconds: float,
    first_sample: Mapping[str, Any] | None = None,
    characteristic_length_m: float | None = None,
) -> dict[str, Any]:
    """Dispatch the first- or second-point preflight without launching tools."""

    if point_index == 0:
        return evaluate_first_point_preflight(
            contract, system_snapshot, timeout_seconds=timeout_seconds
        )
    if point_index == 1 and first_sample is not None:
        return evaluate_second_point_preflight(
            contract,
            system_snapshot,
            first_sample,
            timeout_seconds=timeout_seconds,
            second_characteristic_length_m=characteristic_length_m,
        )
    report = _base_report("UNKNOWN")
    _reason(
        report,
        "POINT_INDEX_INVALID",
        "point_index must be 0, or 1 with a first_sample",
        field="point_index",
    )
    return report


def _existing_disk_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        raise CoarseCalibrationGateError(
            f"cannot find an existing ancestor for disk snapshot path: {path}"
        )
    return candidate


def capture_system_snapshot(
    disk_path: str | os.PathLike[str],
) -> dict[str, int | str]:
    """Capture physical/pagefile availability and disk space without subprocesses."""

    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise CoarseCalibrationGateError(
                f"GlobalMemoryStatusEx failed with Win32 error {ctypes.get_last_error()}"
            )
        physical_total = int(status.ullTotalPhys)
        physical_available = int(status.ullAvailPhys)
        virtual_available = int(status.ullAvailPageFile)
    else:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            physical_total = page_size * int(os.sysconf("SC_PHYS_PAGES"))
            physical_available = page_size * int(os.sysconf("SC_AVPHYS_PAGES"))
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise CoarseCalibrationGateError(
                f"cannot collect POSIX physical-memory snapshot: {error}"
            ) from error
        # The standard library has no portable swap query.  Treat physical
        # availability as the conservative virtual-memory availability; the
        # physical gates remain independently mandatory in all cases.
        virtual_available = physical_available

    resolved_disk = _existing_disk_path(disk_path)
    disk = shutil.disk_usage(resolved_disk)
    return {
        "physical_total_bytes": physical_total,
        "physical_available_bytes": physical_available,
        "virtual_available_bytes": virtual_available,
        "disk_free_bytes": int(disk.free),
        "disk_path": str(resolved_disk),
    }


evaluate_first_calibration_preflight = evaluate_first_point_preflight
evaluate_second_calibration_preflight = evaluate_second_point_preflight


__all__ = [
    "ABSOLUTE_MAXIMUM_TIMEOUT_SECONDS",
    "CALIBRATION_MANIFEST_SCHEMA",
    "CoarseCalibrationGateError",
    "MINIMUM_DISK_FREE_BYTES",
    "REPORT_SCHEMA",
    "capture_system_snapshot",
    "evaluate_coarse_calibration_preflight",
    "evaluate_first_calibration_preflight",
    "evaluate_first_point_preflight",
    "evaluate_second_calibration_preflight",
    "evaluate_second_point_preflight",
]
