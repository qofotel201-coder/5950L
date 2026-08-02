"""Pure, fail-closed preflight for an isolated coarse-mesh projection worker.

The projection worker is deliberately small, but it still imports Gmsh and
constructs a real one-layer mesh in memory.  This module decides whether the
parent may start that worker.  It has no filesystem, process, or Gmsh side
effects and accepts only a normalized coarse-mesh contract plus an exact
resource snapshot.

Physical memory is the hard gate.  Pagefile/virtual availability is recorded
for diagnosis but can never compensate for insufficient immediately available
RAM.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from typing import Any


REPORT_SCHEMA = "cfdpipe.coarse_projection_preflight.v1"
COARSE_CONTRACT_SCHEMA = "cfdpipe.coarse_mesh.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_FIELDS = {
    "physical_total_bytes",
    "physical_available_bytes",
    "virtual_available_bytes",
    "disk_free_bytes",
}
_RESOURCE_FIELDS = (
    "os_physical_memory_reserve_bytes",
    "projection_worker_memory_limit_bytes",
    "worker_monitor_margin_bytes",
    "minimum_disk_free_bytes",
)


def _safe_value(value: Any) -> int | float | str | bool | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return repr(value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reason(
    report: dict[str, Any],
    code: str,
    message: str,
    *,
    field: str | None = None,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        item["field"] = field
    report["reasons"].append(item)


def _base_report() -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "FAIL",
        "stage": "COARSE_PROJECTION_ONLY",
        "safe_to_launch_projection_worker": False,
        "safe_to_import_gmsh_in_worker": False,
        "pagefile_rescue_authorized": False,
        "inputs": {"contract": {}, "system_snapshot": {}},
        "requirements": {},
        "checks": {},
        "reasons": [],
    }


def _normalize_contract(
    report: dict[str, Any], contract: Any
) -> dict[str, int] | None:
    if not isinstance(contract, Mapping):
        report["checks"]["normalized_contract_mapping"] = False
        _reason(
            report,
            "CONTRACT_INVALID",
            "contract must be an already-normalized mapping",
            field="contract",
        )
        return None

    digest = contract.get("normalized_config_sha256")
    schema_pass = contract.get("schema") == COARSE_CONTRACT_SCHEMA
    status_pass = contract.get("status") == "CONFIGURED"
    hash_pass = isinstance(digest, str) and _SHA256.fullmatch(digest) is not None
    report["checks"].update(
        {
            "normalized_contract_mapping": True,
            "normalized_contract_schema": schema_pass,
            "normalized_contract_status": status_pass,
            "normalized_contract_sha256_shape": hash_pass,
        }
    )
    report["inputs"]["contract"] = {
        "schema": _safe_value(contract.get("schema")),
        "status": _safe_value(contract.get("status")),
        "normalized_config_sha256": _safe_value(digest),
        "resource": {},
    }
    if not schema_pass:
        _reason(
            report,
            "CONTRACT_SCHEMA_INVALID",
            "contract schema is not the normalized coarse-mesh schema",
            field="contract.schema",
        )
    if not status_pass:
        _reason(
            report,
            "CONTRACT_STATUS_INVALID",
            "normalized coarse-mesh contract status must be CONFIGURED",
            field="contract.status",
        )
    if not hash_pass:
        _reason(
            report,
            "CONTRACT_SHA256_INVALID",
            "normalized coarse-mesh contract SHA-256 evidence is missing or malformed",
            field="contract.normalized_config_sha256",
        )

    resource = contract.get("resource")
    resource_mapping = isinstance(resource, Mapping)
    report["checks"]["resource_mapping"] = resource_mapping
    if not resource_mapping:
        _reason(
            report,
            "RESOURCE_POLICY_INVALID",
            "normalized contract.resource must be a mapping",
            field="contract.resource",
        )
        return None

    normalized: dict[str, int] = {}
    for field in _RESOURCE_FIELDS:
        value = resource.get(field)
        passed = _positive_int(value)
        report["checks"][f"resource_{field}"] = passed
        report["inputs"]["contract"]["resource"][field] = (
            int(value) if passed else _safe_value(value)
        )
        if passed:
            normalized[field] = int(value)
        else:
            _reason(
                report,
                "RESOURCE_VALUE_INVALID",
                f"contract.resource.{field} must be a positive integer",
                field=f"contract.resource.{field}",
            )

    physical_required = resource.get("physical_memory_required") is True
    pagefile_rejected = (
        resource.get("pagefile_cannot_rescue_physical_memory_failure") is True
    )
    report["checks"]["physical_memory_required_policy"] = physical_required
    report["checks"]["pagefile_rejection_policy"] = pagefile_rejected
    report["inputs"]["contract"]["resource"].update(
        {
            "physical_memory_required": _safe_value(
                resource.get("physical_memory_required")
            ),
            "pagefile_cannot_rescue_physical_memory_failure": _safe_value(
                resource.get("pagefile_cannot_rescue_physical_memory_failure")
            ),
        }
    )
    if not physical_required:
        _reason(
            report,
            "PHYSICAL_MEMORY_POLICY_WEAKENED",
            "projection preflight requires physical_memory_required=true",
            field="contract.resource.physical_memory_required",
        )
    if not pagefile_rejected:
        _reason(
            report,
            "PAGEFILE_POLICY_WEAKENED",
            "projection preflight requires explicit rejection of pagefile rescue",
            field=(
                "contract.resource."
                "pagefile_cannot_rescue_physical_memory_failure"
            ),
        )

    if (
        not schema_pass
        or not status_pass
        or not hash_pass
        or len(normalized) != len(_RESOURCE_FIELDS)
        or not physical_required
        or not pagefile_rejected
    ):
        return None
    return normalized


def _normalize_snapshot(
    report: dict[str, Any], snapshot: Any
) -> dict[str, int] | None:
    if not isinstance(snapshot, Mapping):
        report["checks"]["system_snapshot_mapping"] = False
        _reason(
            report,
            "SYSTEM_SNAPSHOT_INVALID",
            "system snapshot must be a mapping",
            field="system_snapshot",
        )
        return None
    report["checks"]["system_snapshot_mapping"] = True
    exact_keys = set(snapshot) == _SNAPSHOT_FIELDS
    report["checks"]["system_snapshot_exact_keys"] = exact_keys
    if not exact_keys:
        missing = sorted(_SNAPSHOT_FIELDS - set(snapshot))
        extra = sorted(set(snapshot) - _SNAPSHOT_FIELDS)
        _reason(
            report,
            "SYSTEM_SNAPSHOT_KEYS_INVALID",
            f"system snapshot keys are not exact; missing={missing}, extra={extra}",
            field="system_snapshot",
        )

    result: dict[str, int] = {}
    for field in sorted(_SNAPSHOT_FIELDS):
        value = snapshot.get(field)
        validator = _positive_int if field == "physical_total_bytes" else _nonnegative_int
        passed = validator(value)
        report["checks"][f"snapshot_{field}"] = passed
        report["inputs"]["system_snapshot"][field] = (
            int(value) if passed else _safe_value(value)
        )
        if passed:
            result[field] = int(value)
        else:
            qualifier = "positive" if field == "physical_total_bytes" else "nonnegative"
            _reason(
                report,
                "SYSTEM_SNAPSHOT_VALUE_INVALID",
                f"system_snapshot.{field} must be a {qualifier} integer",
                field=f"system_snapshot.{field}",
            )

    if not exact_keys or len(result) != len(_SNAPSHOT_FIELDS):
        return None
    consistent = (
        result["physical_available_bytes"] <= result["physical_total_bytes"]
    )
    report["checks"]["physical_available_not_above_total"] = consistent
    if not consistent:
        _reason(
            report,
            "SYSTEM_SNAPSHOT_INCONSISTENT",
            "available physical memory cannot exceed total physical memory",
            field="system_snapshot.physical_available_bytes",
        )
        return None
    return result


def evaluate_projection_preflight(
    contract: Mapping[str, Any], system_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Return strict authorization evidence for one projection-only worker.

    The decisive RAM formula is::

        available physical >= OS reserve + worker hard limit + monitor margin

    Equality passes.  Virtual/pagefile availability is never added to the
    physical value and is never a substitute for a failed physical check.
    """

    report = _base_report()
    resource = _normalize_contract(report, contract)
    snapshot = _normalize_snapshot(report, system_snapshot)
    if resource is not None:
        required_available = (
            resource["os_physical_memory_reserve_bytes"]
            + resource["projection_worker_memory_limit_bytes"]
            + resource["worker_monitor_margin_bytes"]
        )
        report["requirements"] = {
            "formula": (
                "os_physical_memory_reserve_bytes + "
                "projection_worker_memory_limit_bytes + "
                "worker_monitor_margin_bytes"
            ),
            "physical_available_bytes": required_available,
            "disk_free_bytes": resource["minimum_disk_free_bytes"],
            "pagefile_can_rescue_physical_failure": False,
        }
    else:
        required_available = None

    if resource is not None and snapshot is not None:
        physical_pass = (
            snapshot["physical_available_bytes"] >= required_available
        )
        disk_pass = (
            snapshot["disk_free_bytes"]
            >= resource["minimum_disk_free_bytes"]
        )
        report["checks"].update(
            {
                "physical_available_for_reserve_limit_and_margin": physical_pass,
                "disk_free_minimum": disk_pass,
            }
        )
        if not physical_pass:
            _reason(
                report,
                "PHYSICAL_AVAILABLE_INSUFFICIENT",
                (
                    f"available physical memory {snapshot['physical_available_bytes']} "
                    f"is below required {required_available} bytes"
                ),
                field="system_snapshot.physical_available_bytes",
            )
            if snapshot["virtual_available_bytes"] >= required_available:
                _reason(
                    report,
                    "PAGEFILE_NOT_ACCEPTED",
                    "virtual/pagefile availability cannot rescue the failed physical-memory gate",
                    field="system_snapshot.virtual_available_bytes",
                )
        if not disk_pass:
            _reason(
                report,
                "DISK_FREE_INSUFFICIENT",
                (
                    f"disk free {snapshot['disk_free_bytes']} is below configured "
                    f"minimum {resource['minimum_disk_free_bytes']} bytes"
                ),
                field="system_snapshot.disk_free_bytes",
            )

    passed = (
        resource is not None
        and snapshot is not None
        and not report["reasons"]
    )
    report["status"] = "PASS" if passed else "FAIL"
    report["safe_to_launch_projection_worker"] = passed
    report["safe_to_import_gmsh_in_worker"] = passed
    return report


__all__ = [
    "COARSE_CONTRACT_SCHEMA",
    "REPORT_SCHEMA",
    "evaluate_projection_preflight",
]
