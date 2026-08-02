"""Hash-bound resource report for coarse-mesh calibration artifacts.

This module performs no meshing and launches no external process.  It accepts
only explicit calibration manifests, verifies their complete file lineage, and
feeds measured element count, peak working set, elapsed time and output size to
the conservative resource gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from .resource_gate import evaluate_coarse_resource_gate


class CoarseResourceReportError(RuntimeError):
    """Raised when calibration evidence is incomplete or untrusted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseResourceReportError(f"{label} must be a positive integer")
    return value


def _positive_finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise CoarseResourceReportError(f"{label} must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseResourceReportError(
            f"{label} must be finite and positive"
        ) from error
    if not math.isfinite(number) or number <= 0.0:
        raise CoarseResourceReportError(f"{label} must be finite and positive")
    return number


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoarseResourceReportError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise CoarseResourceReportError(f"{label} is not a JSON object")
    return value


def _trusted_manifest(
    value: str | os.PathLike[str], repository_root: Path
) -> Path:
    lexical = Path(value).expanduser()
    if ".." in lexical.parts:
        raise CoarseResourceReportError("calibration manifest path is unsafe")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise CoarseResourceReportError(
            "calibration manifest does not exist"
        ) from error
    allowed = (repository_root / "runs" / "mesh" / "coarse").resolve(
        strict=False
    )
    if (
        allowed not in resolved.parents
        or not resolved.is_file()
        or resolved.name != "coarse_calibration_manifest.json"
        or lexical.is_symlink()
    ):
        raise CoarseResourceReportError(
            "calibration manifest is outside runs/mesh/coarse or untrusted"
        )
    return resolved


def calibration_sample_from_manifest(
    manifest_path: str | os.PathLike[str],
    *,
    contract: Mapping[str, Any],
    repository_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate one PASS calibration manifest and return a gate sample."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    manifest_file = _trusted_manifest(manifest_path, root)
    manifest = _read_json(manifest_file, "coarse calibration manifest")
    if (
        manifest.get("schema")
        != "cfdpipe.coarse_mesh_calibration_manifest.v1"
        or manifest.get("status") != "PASS"
        or manifest.get("calibration_only") is not True
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
        or manifest.get("external_commands") != []
        or manifest.get("source_unchanged") is not True
        or manifest.get("resource_sample_eligible") is not True
        or manifest.get("coarse_contract_sha256")
        != contract.get("normalized_config_sha256")
    ):
        raise CoarseResourceReportError(
            "coarse calibration manifest status or lineage is invalid"
        )

    characteristic = _positive_finite(
        manifest.get("characteristic_length_m"), "characteristic length"
    )
    authorized = [
        float(item)
        for item in contract.get("calibration_characteristic_lengths_m", [])
    ]
    if not any(
        math.isclose(characteristic, item, rel_tol=1.0e-12, abs_tol=0.0)
        for item in authorized
    ):
        raise CoarseResourceReportError(
            "calibration characteristic length is not authorized"
        )

    sessions = manifest.get("gmsh_sessions")
    if not isinstance(sessions, Mapping) or any(
        not isinstance(sessions.get(phase), Mapping)
        or sessions[phase].get("finalize_called") is not True
        or sessions[phase].get("cleanup_errors") != []
        for phase in ("build", "readback")
    ):
        raise CoarseResourceReportError("calibration Gmsh lifecycle is incomplete")

    generation = manifest.get("generation_audit")
    readback = manifest.get("readback_audit")
    serialized = manifest.get("su2_validation")
    if not all(isinstance(value, Mapping) for value in (generation, readback, serialized)):
        raise CoarseResourceReportError("calibration mesh audits are incomplete")
    elements = _positive_int(generation.get("element_count_3d"), "element count")
    if (
        readback.get("element_count_3d") != elements
        or serialized.get("status") != "PASS"
        or serialized.get("nelem") != elements
        or elements > int(contract.get("calibration_maximum_3d_elements", 0))
    ):
        raise CoarseResourceReportError(
            "calibration generation/readback/SU2 element counts differ"
        )

    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != {"mesh.msh", "mesh.su2"}:
        raise CoarseResourceReportError("calibration output evidence is incomplete")
    output_bytes = 0
    output_evidence: dict[str, dict[str, Any]] = {}
    for name in ("mesh.msh", "mesh.su2"):
        record = outputs.get(name)
        expected = (manifest_file.parent / name).resolve(strict=False)
        if not isinstance(record, Mapping):
            raise CoarseResourceReportError(f"{name} output evidence is invalid")
        try:
            actual = Path(str(record.get("path", ""))).resolve(strict=True)
        except OSError as error:
            raise CoarseResourceReportError(f"{name} output is missing") from error
        size = _positive_int(record.get("size_bytes"), f"{name} size")
        if (
            actual != expected
            or not actual.is_file()
            or actual.stat().st_size != size
            or record.get("sha256") != _sha256(actual)
        ):
            raise CoarseResourceReportError(f"{name} output hash/size/path is stale")
        output_bytes += size
        output_evidence[name] = {
            "path": str(actual),
            "size_bytes": size,
            "sha256": record["sha256"],
        }

    return {
        "manifest_path": str(manifest_file),
        "manifest_sha256": _sha256(manifest_file),
        "characteristic_length_m": characteristic,
        "elements": elements,
        "peak_working_set_bytes": _positive_int(
            manifest.get("peak_working_set_bytes"), "peak working set"
        ),
        "elapsed_seconds": _positive_finite(
            manifest.get("elapsed_seconds"), "elapsed time"
        ),
        "output_bytes": output_bytes,
        "outputs": output_evidence,
    }


def system_resource_snapshot(
    repository_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Return current physical, virtual and disk availability using stdlib APIs."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    if os.name == "nt":
        import ctypes

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
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise CoarseResourceReportError("GlobalMemoryStatusEx failed")
        physical_total = int(status.ullTotalPhys)
        physical_available = int(status.ullAvailPhys)
        virtual_available = int(status.ullAvailPageFile)
        memory_source = "Windows GlobalMemoryStatusEx"
    else:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            physical_total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
            physical_available = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
        except (AttributeError, OSError, ValueError) as error:
            raise CoarseResourceReportError(
                "cannot query POSIX physical memory"
            ) from error
        virtual_available = physical_available
        memory_source = "POSIX sysconf (virtual availability conservatively equals RAM)"
    disk = shutil.disk_usage(root)
    return {
        "captured_at_utc": _utc_now(),
        "memory_source": memory_source,
        "disk_source_path": str(root),
        "physical_total_bytes": physical_total,
        "physical_available_bytes": physical_available,
        "virtual_available_bytes": virtual_available,
        "disk_free_bytes": int(disk.free),
    }


def build_coarse_resource_report(
    *,
    contract: Mapping[str, Any],
    calibration_manifests: Sequence[str | os.PathLike[str]],
    repository_root: str | os.PathLike[str],
    resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a strict resource-gate report from every authorized calibration."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    samples = [
        calibration_sample_from_manifest(
            path, contract=contract, repository_root=root
        )
        for path in calibration_manifests
    ]
    expected_sizes = sorted(
        float(value)
        for value in contract.get("calibration_characteristic_lengths_m", [])
    )
    actual_sizes = sorted(sample["characteristic_length_m"] for sample in samples)
    if len(actual_sizes) != len(set(actual_sizes)) or actual_sizes != expected_sizes:
        raise CoarseResourceReportError(
            "resource report requires each authorized calibration exactly once"
        )
    snapshot = dict(resources or system_resource_snapshot(root))
    required_resource_keys = {
        "physical_total_bytes",
        "physical_available_bytes",
        "virtual_available_bytes",
        "disk_free_bytes",
    }
    if not required_resource_keys <= set(snapshot):
        raise CoarseResourceReportError("system resource snapshot is incomplete")
    gate = evaluate_coarse_resource_gate(
        samples,
        target_elements=int(contract["target_3d_elements"]),
        physical_total_bytes=snapshot["physical_total_bytes"],
        physical_available_bytes=snapshot["physical_available_bytes"],
        virtual_available_bytes=snapshot["virtual_available_bytes"],
        disk_free_bytes=snapshot["disk_free_bytes"],
        maximum_elapsed_seconds=None,
    )
    minimum_disk = int(contract.get("resource", {}).get("minimum_disk_free_bytes", 0))
    if snapshot["disk_free_bytes"] < minimum_disk:
        gate["status"] = "FAIL"
        gate["checks"]["configured_minimum_disk_free"] = False
        gate["reasons"].append(
            {
                "code": "CONFIGURED_MINIMUM_DISK_FREE_INSUFFICIENT",
                "message": (
                    f"disk free {snapshot['disk_free_bytes']} is below configured "
                    f"minimum {minimum_disk}"
                ),
                "field": "disk_free_bytes",
            }
        )
    else:
        gate["checks"]["configured_minimum_disk_free"] = True
    return {
        "schema": "cfdpipe.coarse_resource_report.v1",
        "status": gate["status"],
        "production_mesh_authorized": gate["status"] == "PASS",
        "created_at_utc": _utc_now(),
        "coarse_contract_sha256": contract["normalized_config_sha256"],
        "calibration_samples": samples,
        "system_resources": snapshot,
        "resource_gate": gate,
    }


__all__ = [
    "CoarseResourceReportError",
    "build_coarse_resource_report",
    "calibration_sample_from_manifest",
    "system_resource_snapshot",
]
