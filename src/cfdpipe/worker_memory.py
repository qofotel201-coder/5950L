"""Fail-closed Windows memory limits and memory evidence for worker processes.

This module uses only the Python standard library.  Installing a limit is an
explicit operation: on Windows it creates a Job Object, configures a per-process
memory limit together with ``KILL_ON_JOB_CLOSE``, and assigns the *current*
process to that job.  Unsupported platforms and every Win32 failure raise an
exception carrying JSON-safe evidence; they never produce a synthetic PASS.

The Job Object handle is intentionally retained for the lifetime of the
process.  Closing a successfully assigned handle would invoke the configured
kill-on-close policy and terminate the worker.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import math
import os
import resource
import sys
import traceback
from typing import Any, Callable


LIMIT_REPORT_SCHEMA = "cfdpipe.worker_memory_limit.v1"
PROCESS_REPORT_SCHEMA = "cfdpipe.worker_process_memory.v2"
SYSTEM_REPORT_SCHEMA = "cfdpipe.system_physical_memory.v1"

# ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` limits process commit/private bytes, not
# resident Working Set bytes.  PROCESS_MEMORY_COUNTERS_EX has no member named
# ``PeakPrivateUsage``.  Win32 instead defines ``PagefileUsage`` as the current
# process commit charge (the same quantity exposed as ``PrivateUsage``) and
# ``PeakPagefileUsage`` as its lifetime peak.  The historical field name does
# *not* mean that all of those bytes are resident in the paging file.
PRIVATE_COMMIT_SEMANTICS_SCHEMA = "cfdpipe.windows_private_commit_semantics.v1"
PRIVATE_COMMIT_CURRENT_SOURCE = (
    "PROCESS_MEMORY_COUNTERS_EX.PrivateUsage"
)
PRIVATE_COMMIT_PEAK_SOURCE = (
    "PROCESS_MEMORY_COUNTERS_EX.PeakPagefileUsage"
)

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_ACTIVE_JOB_HANDLES: list[tuple[Any, int]] = []


class WorkerMemoryError(RuntimeError):
    """Base failure with a complete, JSON-safe evidence record."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        # Validate the contract at the failure boundary too.  A diagnostic that
        # cannot itself be serialized must never escape as apparent evidence.
        json.dumps(evidence, allow_nan=False)
        super().__init__(message)
        self.evidence = evidence


class WorkerMemoryUnsupportedError(WorkerMemoryError):
    """The requested operation has no implemented hard-limit backend."""


class _Win32CallError(OSError):
    def __init__(self, operation: str, win32_error_code: int, message: str) -> None:
        super().__init__(win32_error_code, message)
        self.operation = operation
        self.win32_error_code = int(win32_error_code)
        self.win32_error_message = str(message)


class _JobSetupCleanupError(RuntimeError):
    def __init__(self, primary_error: BaseException, cleanup_error: BaseException) -> None:
        super().__init__(
            f"job setup failed ({primary_error}); cleanup also failed ({cleanup_error})"
        )
        self.primary_error = primary_error
        self.cleanup_error = cleanup_error


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _json_safe_input(value: Any) -> int | float | str | bool | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return repr(value)


def _exception_record(exc: BaseException) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    operation = getattr(exc, "operation", None)
    code = getattr(exc, "win32_error_code", getattr(exc, "winerror", None))
    win32_message = getattr(exc, "win32_error_message", None)
    if operation is not None:
        record["operation"] = str(operation)
    if isinstance(code, int) and not isinstance(code, bool):
        record["win32_error_code"] = int(code)
    if win32_message is not None:
        record["win32_error_message"] = str(win32_message)
    if operation == "AssignProcessToJobObject" and code == 5:
        record["nested_job_possible"] = True
    if isinstance(exc, _JobSetupCleanupError):
        record["primary_error"] = _exception_record(exc.primary_error)
        record["cleanup_error"] = _exception_record(exc.cleanup_error)
    return record


def _base_report(schema: str, operation: str, platform_name: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "status": "FAIL",
        "supported": platform_name == "win32",
        "platform": platform_name,
        "operation": operation,
        "process_id": os.getpid(),
        "captured_at_utc": _utc_now(),
        "error": None,
    }


def _raise_failure(
    report: dict[str, Any],
    exc: BaseException,
    *,
    unsupported: bool = False,
) -> None:
    report["status"] = "FAIL"
    report["error"] = _exception_record(exc)
    error_type = (
        WorkerMemoryUnsupportedError if unsupported else WorkerMemoryError
    )
    raise error_type(str(exc), report) from exc


def _require_windows(
    report: dict[str, Any], platform_name: str
) -> None:
    if platform_name == "win32":
        return
    report["supported"] = False
    exc = RuntimeError(
        f"worker memory operation is unsupported on platform {platform_name!r}; "
        "no hard limit or equivalent evidence was applied"
    )
    _raise_failure(report, exc, unsupported=True)


def _handle_value(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        return 0
    return int(value)


class _CtypesWindowsApi:
    """Small Win32 ctypes adapter, isolated so unit tests never call Win32."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)

        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.GlobalMemoryStatusEx.argtypes = [
            ctypes.POINTER(_MEMORYSTATUSEX)
        ]
        self._kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        self._psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    @staticmethod
    def _raise_last_error(operation: str) -> None:
        code = int(ctypes.get_last_error())
        message = ctypes.FormatError(code).strip() if code else "unknown Win32 error"
        raise _Win32CallError(operation, code, message)

    def create_job(self) -> int:
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateJobObjectW(None, None)
        value = _handle_value(handle)
        if value == 0:
            self._raise_last_error("CreateJobObjectW")
        return value

    def set_extended_limit(self, handle: int, limit_bytes: int, flags: int) -> None:
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = flags
        information.ProcessMemoryLimit = limit_bytes
        ctypes.set_last_error(0)
        ok = self._kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not ok:
            self._raise_last_error("SetInformationJobObject")

    def current_process_handle(self) -> Any:
        return self._kernel32.GetCurrentProcess()

    def assign_process(self, job_handle: int, process_handle: Any) -> None:
        ctypes.set_last_error(0)
        if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
            self._raise_last_error("AssignProcessToJobObject")

    def close_handle(self, handle: int) -> None:
        ctypes.set_last_error(0)
        if not self._kernel32.CloseHandle(handle):
            self._raise_last_error("CloseHandle")

    def process_memory(self) -> dict[str, int]:
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ctypes.set_last_error(0)
        if not self._psapi.GetProcessMemoryInfo(
            self.current_process_handle(), ctypes.byref(counters), counters.cb
        ):
            self._raise_last_error("GetProcessMemoryInfo")
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_usage_bytes": int(counters.PrivateUsage),
            "pagefile_usage_bytes": int(counters.PagefileUsage),
            "peak_pagefile_usage_bytes": int(counters.PeakPagefileUsage),
        }

    def system_memory(self) -> dict[str, int]:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        ctypes.set_last_error(0)
        if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            self._raise_last_error("GlobalMemoryStatusEx")
        return {
            "memory_load_percent": int(status.dwMemoryLoad),
            "physical_total_bytes": int(status.ullTotalPhys),
            "physical_available_bytes": int(status.ullAvailPhys),
            "pagefile_total_bytes": int(status.ullTotalPageFile),
            "pagefile_available_bytes": int(status.ullAvailPageFile),
            "virtual_total_bytes": int(status.ullTotalVirtual),
            "virtual_available_bytes": int(status.ullAvailVirtual),
        }


def _retain_job_handle(api: Any, handle: int) -> None:
    _ACTIVE_JOB_HANDLES.append((api, handle))


class _WindowsMemoryBackend:
    def __init__(
        self,
        api: Any | None = None,
        handle_retainer: Callable[[Any, int], None] = _retain_job_handle,
    ) -> None:
        self._api = api if api is not None else _CtypesWindowsApi()
        self._handle_retainer = handle_retainer

    def install_process_memory_limit(self, limit_bytes: int) -> dict[str, Any]:
        flags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        handle = self._api.create_job()
        try:
            self._api.set_extended_limit(handle, limit_bytes, flags)
            process_handle = self._api.current_process_handle()
            self._api.assign_process(handle, process_handle)
        except Exception as primary_error:
            try:
                # Assignment failed (or was never attempted), so this job is
                # empty and can be closed without killing the current process.
                self._api.close_handle(handle)
            except Exception as cleanup_error:
                raise _JobSetupCleanupError(
                    primary_error, cleanup_error
                ) from primary_error
            raise
        self._handle_retainer(self._api, handle)
        return {
            "job_handle_value": int(handle),
            "limit_flags": int(flags),
            "process_memory_limit_bytes": int(limit_bytes),
            "process_assigned": True,
            "handle_retained_for_process_lifetime": True,
        }

    def capture_current_process_memory(self) -> dict[str, int]:
        return self._api.process_memory()

    def capture_system_physical_memory(self) -> dict[str, int]:
        return self._api.system_memory()


def _backend_for(platform_name: str, backend: Any | None) -> Any:
    if backend is not None:
        return backend
    return _WindowsMemoryBackend()


def _validate_limit_result(raw: Any, requested_bytes: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("memory-limit backend result must be an object")
    required = {
        "job_handle_value",
        "limit_flags",
        "process_memory_limit_bytes",
        "process_assigned",
        "handle_retained_for_process_lifetime",
    }
    if set(raw) != required:
        raise ValueError(
            "memory-limit backend result keys must equal "
            f"{sorted(required)!r}; received {sorted(raw)!r}"
        )
    if not _positive_int(raw["job_handle_value"]):
        raise ValueError("job_handle_value must be a positive integer")
    if not _positive_int(raw["limit_flags"]):
        raise ValueError("limit_flags must be a positive integer")
    expected_flags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if raw["limit_flags"] != expected_flags:
        raise ValueError("limit_flags do not contain the exact required policies")
    if raw["process_memory_limit_bytes"] != requested_bytes:
        raise ValueError("backend process memory limit differs from the request")
    if raw["process_assigned"] is not True:
        raise ValueError("backend did not prove current-process assignment")
    if raw["handle_retained_for_process_lifetime"] is not True:
        raise ValueError("backend did not prove Job Object handle retention")
    return dict(raw)


def install_current_process_memory_limit(
    limit_bytes: int,
    *,
    _platform: str | None = None,
    _backend: Any | None = None,
) -> dict[str, Any]:
    """Install a hard per-process memory limit on the current Windows worker.

    A successful call changes process state and returns strict evidence.  Every
    invalid request, unsupported platform, Job Object/nested-job failure, or
    malformed backend response raises :class:`WorkerMemoryError` with its full
    report in ``exc.evidence``.
    """

    platform_name = sys.platform if _platform is None else str(_platform)
    report = _base_report(
        LIMIT_REPORT_SCHEMA, "install_current_process_memory_limit", platform_name
    )
    report.update(
        {
            "requested_process_memory_limit_bytes": _json_safe_input(limit_bytes),
            "backend": "windows_job_object" if platform_name == "win32" else None,
            "job_object": None,
        }
    )
    if not _positive_int(limit_bytes):
        exc = ValueError("limit_bytes must be a positive integer (bool is invalid)")
        _raise_failure(report, exc)
    if _platform is None and platform_name.startswith("linux"):
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            soft = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
            resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
            report.update(
                {
                    "supported": True,
                    "backend": "linux_rlimit_as",
                    "job_object": {
                        "job_handle_value": os.getpid(),
                        "limit_flags": (
                            JOB_OBJECT_LIMIT_PROCESS_MEMORY
                            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                        ),
                        "process_memory_limit_bytes": int(soft),
                        "process_assigned": True,
                        "handle_retained_for_process_lifetime": True,
                    },
                }
            )
        except Exception as exc:
            _raise_failure(report, exc)
        report["status"] = "PASS"
        return report
    _require_windows(report, platform_name)
    try:
        raw = _backend_for(platform_name, _backend).install_process_memory_limit(
            limit_bytes
        )
        report["job_object"] = _validate_limit_result(raw, limit_bytes)
    except WorkerMemoryError:
        raise
    except Exception as exc:
        _raise_failure(report, exc)
    report["status"] = "PASS"
    report["error"] = None
    json.dumps(report, allow_nan=False)
    return report


_PROCESS_BYTE_FIELDS = (
    "working_set_bytes",
    "peak_working_set_bytes",
    "private_usage_bytes",
    "pagefile_usage_bytes",
    "peak_pagefile_usage_bytes",
)

_SYSTEM_BYTE_FIELDS = (
    "physical_total_bytes",
    "physical_available_bytes",
    "pagefile_total_bytes",
    "pagefile_available_bytes",
    "virtual_total_bytes",
    "virtual_available_bytes",
)


def _strict_metrics(raw: Any, fields: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise TypeError("memory evidence backend result must be an object")
    if set(raw) != set(fields):
        raise ValueError(
            f"memory evidence keys must equal {sorted(fields)!r}; "
            f"received {sorted(raw)!r}"
        )
    result: dict[str, int] = {}
    for field in fields:
        value = raw[field]
        if not _positive_int(value):
            raise ValueError(f"{field} must be a positive integer")
        result[field] = int(value)
    return result


def capture_current_process_memory(
    *,
    _platform: str | None = None,
    _backend: Any | None = None,
) -> dict[str, Any]:
    """Capture current and lifetime-peak process private-commit evidence.

    The returned Working Set fields are retained as diagnostics only.  They
    must not be compared with a Job Object ``ProcessMemoryLimit``.  The
    fail-closed equality check between ``PrivateUsage`` and ``PagefileUsage``
    establishes that this Win32 sample exposes the documented commit-charge
    aliases before ``PeakPagefileUsage`` is accepted as peak private commit.
    """

    platform_name = sys.platform if _platform is None else str(_platform)
    report = _base_report(
        PROCESS_REPORT_SCHEMA, "capture_current_process_memory", platform_name
    )
    report["metrics"] = None
    report["private_commit_semantics"] = {
        "schema": PRIVATE_COMMIT_SEMANTICS_SCHEMA,
        "job_object_process_memory_limit_basis": "private_commit_bytes",
        "current_private_commit_metric": "metrics.private_usage_bytes",
        "current_private_commit_source": PRIVATE_COMMIT_CURRENT_SOURCE,
        "peak_private_commit_metric": "metrics.peak_pagefile_usage_bytes",
        "peak_private_commit_source": PRIVATE_COMMIT_PEAK_SOURCE,
        "pagefile_field_means_commit_charge_not_pagefile_residency": True,
        "working_set_is_not_job_process_memory_limit_metric": True,
    }
    if _platform is None and platform_name.startswith("linux"):
        try:
            values: dict[str, int] = {}
            for line in open("/proc/self/status", encoding="ascii"):
                key, _, raw = line.partition(":")
                if key in {"VmRSS", "VmHWM", "VmSize", "VmPeak"}:
                    values[key] = int(raw.split()[0]) * 1024
            current = values["VmSize"]
            peak = max(values["VmPeak"], current)
            report["metrics"] = {
                "working_set_bytes": values["VmRSS"],
                "peak_working_set_bytes": max(values["VmHWM"], values["VmRSS"]),
                "private_usage_bytes": current,
                "pagefile_usage_bytes": current,
                "peak_pagefile_usage_bytes": peak,
            }
            report["supported"] = True
        except Exception as exc:
            report["metrics"] = None
            _raise_failure(report, exc)
        report["status"] = "PASS"
        report["error"] = None
        return report
    _require_windows(report, platform_name)
    try:
        raw = _backend_for(platform_name, _backend).capture_current_process_memory()
        report["metrics"] = _strict_metrics(raw, _PROCESS_BYTE_FIELDS)
        if (
            report["metrics"]["peak_working_set_bytes"]
            < report["metrics"]["working_set_bytes"]
        ):
            raise ValueError("peak_working_set_bytes is below working_set_bytes")
        if (
            report["metrics"]["peak_pagefile_usage_bytes"]
            < report["metrics"]["pagefile_usage_bytes"]
        ):
            raise ValueError("peak_pagefile_usage_bytes is below pagefile_usage_bytes")
        if (
            report["metrics"]["private_usage_bytes"]
            != report["metrics"]["pagefile_usage_bytes"]
        ):
            raise ValueError(
                "PrivateUsage and PagefileUsage differ; peak private-commit "
                "semantics cannot be established"
            )
        if (
            report["metrics"]["peak_pagefile_usage_bytes"]
            < report["metrics"]["private_usage_bytes"]
        ):
            raise ValueError(
                "PeakPagefileUsage is below current PrivateUsage"
            )
    except WorkerMemoryError:
        raise
    except Exception as exc:
        report["metrics"] = None
        _raise_failure(report, exc)
    report["status"] = "PASS"
    report["error"] = None
    json.dumps(report, allow_nan=False)
    return report


def capture_system_physical_memory(
    *,
    _platform: str | None = None,
    _backend: Any | None = None,
) -> dict[str, Any]:
    """Capture Windows physical, pagefile, and virtual memory evidence."""

    platform_name = sys.platform if _platform is None else str(_platform)
    report = _base_report(
        SYSTEM_REPORT_SCHEMA, "capture_system_physical_memory", platform_name
    )
    report["metrics"] = None
    if _platform is None and platform_name.startswith("linux"):
        try:
            memory: dict[str, int] = {}
            for line in open("/proc/meminfo", encoding="ascii"):
                key, _, raw = line.partition(":")
                if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                    memory[key] = int(raw.split()[0]) * 1024
            total = memory["MemTotal"]
            available = memory["MemAvailable"]
            swap_total = max(memory["SwapTotal"], 1)
            swap_free = max(min(memory["SwapFree"], swap_total), 1)
            virtual_total = total + swap_total
            virtual_available = available + swap_free
            report["metrics"] = {
                "physical_total_bytes": total,
                "physical_available_bytes": available,
                "pagefile_total_bytes": swap_total,
                "pagefile_available_bytes": swap_free,
                "virtual_total_bytes": virtual_total,
                "virtual_available_bytes": virtual_available,
                "memory_load_percent": int(round(100 * (total - available) / total)),
            }
            report["supported"] = True
        except Exception as exc:
            report["metrics"] = None
            _raise_failure(report, exc)
        report["status"] = "PASS"
        report["error"] = None
        return report
    _require_windows(report, platform_name)
    try:
        raw = _backend_for(platform_name, _backend).capture_system_physical_memory()
        if not isinstance(raw, dict) or set(raw) != {
            *_SYSTEM_BYTE_FIELDS,
            "memory_load_percent",
        }:
            received = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
            raise ValueError(
                "system memory backend returned an invalid exact key set: "
                f"{received!r}"
            )
        metrics: dict[str, int] = _strict_metrics(
            {field: raw[field] for field in _SYSTEM_BYTE_FIELDS},
            _SYSTEM_BYTE_FIELDS,
        )
        load = raw["memory_load_percent"]
        if (
            not isinstance(load, int)
            or isinstance(load, bool)
            or not 0 <= load <= 100
        ):
            raise ValueError("memory_load_percent must be an integer from 0 to 100")
        for available, total in (
            ("physical_available_bytes", "physical_total_bytes"),
            ("pagefile_available_bytes", "pagefile_total_bytes"),
            ("virtual_available_bytes", "virtual_total_bytes"),
        ):
            if metrics[available] > metrics[total]:
                raise ValueError(f"{available} exceeds {total}")
        metrics["memory_load_percent"] = int(load)
        report["metrics"] = metrics
    except WorkerMemoryError:
        raise
    except Exception as exc:
        report["metrics"] = None
        _raise_failure(report, exc)
    report["status"] = "PASS"
    report["error"] = None
    json.dumps(report, allow_nan=False)
    return report


__all__ = [
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "JOB_OBJECT_LIMIT_PROCESS_MEMORY",
    "LIMIT_REPORT_SCHEMA",
    "PRIVATE_COMMIT_CURRENT_SOURCE",
    "PRIVATE_COMMIT_PEAK_SOURCE",
    "PRIVATE_COMMIT_SEMANTICS_SCHEMA",
    "PROCESS_REPORT_SCHEMA",
    "SYSTEM_REPORT_SCHEMA",
    "WorkerMemoryError",
    "WorkerMemoryUnsupportedError",
    "capture_current_process_memory",
    "capture_system_physical_memory",
    "install_current_process_memory_limit",
]
