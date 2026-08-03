from __future__ import annotations

import json
import math
import unittest

from cfdpipe.worker_memory import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    PRIVATE_COMMIT_PEAK_SOURCE,
    PRIVATE_COMMIT_SEMANTICS_SCHEMA,
    PROCESS_REPORT_SCHEMA,
    WorkerMemoryError,
    WorkerMemoryUnsupportedError,
    _Win32CallError,
    _WindowsMemoryBackend,
    capture_current_process_memory,
    capture_system_physical_memory,
    install_current_process_memory_limit,
)


MIB = 1024**2
GIB = 1024**3
REQUIRED_FLAGS = (
    JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
)


class FakeBackend:
    def __init__(self) -> None:
        self.limit_calls: list[int] = []
        self.limit_error: BaseException | None = None
        self.process_error: BaseException | None = None
        self.system_error: BaseException | None = None
        self.limit_result = {
            "job_handle_value": 123,
            "limit_flags": REQUIRED_FLAGS,
            "process_memory_limit_bytes": 2 * GIB,
            "process_assigned": True,
            "handle_retained_for_process_lifetime": True,
        }
        self.process_result = {
            "working_set_bytes": 100 * MIB,
            "peak_working_set_bytes": 120 * MIB,
            "private_usage_bytes": 80 * MIB,
            "pagefile_usage_bytes": 80 * MIB,
            "peak_pagefile_usage_bytes": 90 * MIB,
        }
        self.system_result = {
            "memory_load_percent": 50,
            "physical_total_bytes": 16 * GIB,
            "physical_available_bytes": 8 * GIB,
            "pagefile_total_bytes": 24 * GIB,
            "pagefile_available_bytes": 10 * GIB,
            "virtual_total_bytes": 128 * GIB,
            "virtual_available_bytes": 100 * GIB,
        }

    def install_process_memory_limit(self, limit_bytes: int):
        self.limit_calls.append(limit_bytes)
        if self.limit_error is not None:
            raise self.limit_error
        result = dict(self.limit_result)
        result["process_memory_limit_bytes"] = limit_bytes
        return result

    def capture_current_process_memory(self):
        if self.process_error is not None:
            raise self.process_error
        return dict(self.process_result)

    def capture_system_physical_memory(self):
        if self.system_error is not None:
            raise self.system_error
        return dict(self.system_result)


class FakeWin32Api:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.failure_operation: str | None = None
        self.close_failure = False

    def _maybe_fail(self, operation: str) -> None:
        if self.failure_operation == operation:
            code = 5 if operation == "AssignProcessToJobObject" else 87
            raise _Win32CallError(operation, code, f"raw {operation} error")

    def create_job(self) -> int:
        self.calls.append(("create_job",))
        self._maybe_fail("CreateJobObjectW")
        return 456

    def set_extended_limit(self, handle: int, limit_bytes: int, flags: int) -> None:
        self.calls.append(("set_extended_limit", handle, limit_bytes, flags))
        self._maybe_fail("SetInformationJobObject")

    def current_process_handle(self) -> int:
        self.calls.append(("current_process_handle",))
        return 789

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        self.calls.append(("assign_process", job_handle, process_handle))
        self._maybe_fail("AssignProcessToJobObject")

    def close_handle(self, handle: int) -> None:
        self.calls.append(("close_handle", handle))
        if self.close_failure:
            raise _Win32CallError("CloseHandle", 6, "raw close error")


class WorkerMemoryLimitTests(unittest.TestCase):
    def test_limit_forwards_exact_positive_bytes_and_is_json_safe(self) -> None:
        backend = FakeBackend()

        report = install_current_process_memory_limit(
            2 * GIB, _platform="win32", _backend=backend
        )

        self.assertEqual([2 * GIB], backend.limit_calls)
        self.assertEqual("PASS", report["status"])
        self.assertTrue(report["supported"])
        self.assertEqual(REQUIRED_FLAGS, report["job_object"]["limit_flags"])
        self.assertTrue(report["job_object"]["process_assigned"])
        self.assertTrue(
            report["job_object"]["handle_retained_for_process_lifetime"]
        )
        json.dumps(report, allow_nan=False)

    def test_non_positive_non_integer_and_bool_limits_are_rejected_before_backend(self) -> None:
        for invalid in (0, -1, True, 1.5, math.nan, math.inf, "1024", object()):
            with self.subTest(invalid=invalid):
                backend = FakeBackend()
                with self.assertRaises(WorkerMemoryError) as caught:
                    install_current_process_memory_limit(
                        invalid, _platform="win32", _backend=backend
                    )
                self.assertEqual([], backend.limit_calls)
                self.assertEqual("FAIL", caught.exception.evidence["status"])
                self.assertEqual("ValueError", caught.exception.evidence["error"]["type"])
                json.dumps(caught.exception.evidence, allow_nan=False)

    def test_non_windows_is_explicitly_unsupported(self) -> None:
        backend = FakeBackend()
        with self.assertRaises(WorkerMemoryUnsupportedError) as caught:
            install_current_process_memory_limit(
                GIB, _platform="linux", _backend=backend
            )

        evidence = caught.exception.evidence
        self.assertFalse(evidence["supported"])
        self.assertEqual("FAIL", evidence["status"])
        self.assertIn("unsupported", evidence["error"]["message"])
        self.assertEqual([], backend.limit_calls)

    def test_assign_failure_preserves_raw_nested_job_error_and_cause(self) -> None:
        backend = FakeBackend()
        raw = _Win32CallError("AssignProcessToJobObject", 5, "Access is denied")
        backend.limit_error = raw

        with self.assertRaises(WorkerMemoryError) as caught:
            install_current_process_memory_limit(
                GIB, _platform="win32", _backend=backend
            )

        error = caught.exception.evidence["error"]
        self.assertEqual("AssignProcessToJobObject", error["operation"])
        self.assertEqual(5, error["win32_error_code"])
        self.assertEqual("Access is denied", error["win32_error_message"])
        self.assertTrue(error["nested_job_possible"])
        self.assertIs(raw, caught.exception.__cause__)
        self.assertIn("_Win32CallError", error["traceback"])

    def test_internal_backend_sets_both_flags_before_assignment_and_retains_handle(self) -> None:
        api = FakeWin32Api()
        retained: list[tuple[object, int]] = []
        backend = _WindowsMemoryBackend(
            api=api, handle_retainer=lambda owner, handle: retained.append((owner, handle))
        )

        result = backend.install_process_memory_limit(3 * GIB)

        self.assertEqual(
            [
                ("create_job",),
                ("set_extended_limit", 456, 3 * GIB, REQUIRED_FLAGS),
                ("current_process_handle",),
                ("assign_process", 456, 789),
            ],
            api.calls,
        )
        self.assertEqual([(api, 456)], retained)
        self.assertEqual(REQUIRED_FLAGS, result["limit_flags"])
        self.assertNotIn(("close_handle", 456), api.calls)

    def test_internal_backend_assignment_failure_closes_empty_job_and_propagates(self) -> None:
        api = FakeWin32Api()
        api.failure_operation = "AssignProcessToJobObject"
        retained: list[tuple[object, int]] = []
        backend = _WindowsMemoryBackend(
            api=api, handle_retainer=lambda owner, handle: retained.append((owner, handle))
        )

        with self.assertRaises(_Win32CallError) as caught:
            backend.install_process_memory_limit(GIB)

        self.assertEqual(5, caught.exception.win32_error_code)
        self.assertEqual(("close_handle", 456), api.calls[-1])
        self.assertEqual([], retained)

    def test_malformed_success_evidence_fails_closed(self) -> None:
        backend = FakeBackend()
        backend.limit_result["process_assigned"] = False

        with self.assertRaises(WorkerMemoryError) as caught:
            install_current_process_memory_limit(
                2 * GIB, _platform="win32", _backend=backend
            )

        self.assertEqual("FAIL", caught.exception.evidence["status"])
        self.assertIn("did not prove", caught.exception.evidence["error"]["message"])


class WorkerMemoryEvidenceTests(unittest.TestCase):
    def test_current_process_memory_is_strict_positive_and_json_safe(self) -> None:
        report = capture_current_process_memory(
            _platform="win32", _backend=FakeBackend()
        )

        self.assertEqual("PASS", report["status"])
        self.assertEqual(PROCESS_REPORT_SCHEMA, report["schema"])
        self.assertEqual(5, len(report["metrics"]))
        for name, value in report["metrics"].items():
            self.assertTrue(name.endswith("_bytes"))
            self.assertIs(type(value), int)
            self.assertGreater(value, 0)
        semantics = report["private_commit_semantics"]
        self.assertEqual(PRIVATE_COMMIT_SEMANTICS_SCHEMA, semantics["schema"])
        self.assertEqual(
            PRIVATE_COMMIT_PEAK_SOURCE,
            semantics["peak_private_commit_source"],
        )
        self.assertEqual(
            "metrics.peak_pagefile_usage_bytes",
            semantics["peak_private_commit_metric"],
        )
        self.assertTrue(
            semantics["working_set_is_not_job_process_memory_limit_metric"]
        )
        self.assertTrue(
            semantics[
                "pagefile_field_means_commit_charge_not_pagefile_residency"
            ]
        )
        json.dumps(report, allow_nan=False)

    def test_current_process_invalid_or_inconsistent_bytes_fail_closed(self) -> None:
        for field, value in (
            ("working_set_bytes", 0),
            ("private_usage_bytes", True),
            ("peak_working_set_bytes", 50 * MIB),
            ("peak_pagefile_usage_bytes", 1),
        ):
            with self.subTest(field=field):
                backend = FakeBackend()
                backend.process_result[field] = value
                with self.assertRaises(WorkerMemoryError) as caught:
                    capture_current_process_memory(
                        _platform="win32", _backend=backend
                    )
                self.assertIsNone(caught.exception.evidence["metrics"])
                json.dumps(caught.exception.evidence, allow_nan=False)

    def test_private_usage_and_pagefile_commit_aliases_must_match(self) -> None:
        backend = FakeBackend()
        backend.process_result["pagefile_usage_bytes"] += 1

        with self.assertRaises(WorkerMemoryError) as caught:
            capture_current_process_memory(_platform="win32", _backend=backend)

        evidence = caught.exception.evidence
        self.assertIsNone(evidence["metrics"])
        self.assertIn("PrivateUsage and PagefileUsage differ", evidence["error"]["message"])
        self.assertEqual(
            "metrics.peak_pagefile_usage_bytes",
            evidence["private_commit_semantics"]["peak_private_commit_metric"],
        )

    def test_system_memory_is_strict_and_json_safe(self) -> None:
        report = capture_system_physical_memory(
            _platform="win32", _backend=FakeBackend()
        )

        self.assertEqual("PASS", report["status"])
        self.assertEqual(50, report["metrics"]["memory_load_percent"])
        for name, value in report["metrics"].items():
            self.assertIs(type(value), int)
            if name.endswith("_bytes"):
                self.assertGreater(value, 0)
        json.dumps(report, allow_nan=False)

    def test_system_memory_rejects_zero_bool_and_available_above_total(self) -> None:
        mutations = (
            ("physical_available_bytes", 0),
            ("physical_total_bytes", True),
            ("physical_available_bytes", 17 * GIB),
            ("memory_load_percent", 101),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                backend = FakeBackend()
                backend.system_result[field] = value
                with self.assertRaises(WorkerMemoryError) as caught:
                    capture_system_physical_memory(
                        _platform="win32", _backend=backend
                    )
                self.assertIsNone(caught.exception.evidence["metrics"])

    def test_capture_backend_failure_is_not_swallowed(self) -> None:
        backend = FakeBackend()
        raw = _Win32CallError("GetProcessMemoryInfo", 299, "partial copy")
        backend.process_error = raw

        with self.assertRaises(WorkerMemoryError) as caught:
            capture_current_process_memory(
                _platform="win32", _backend=backend
            )

        self.assertIs(raw, caught.exception.__cause__)
        error = caught.exception.evidence["error"]
        self.assertEqual(299, error["win32_error_code"])
        self.assertEqual("GetProcessMemoryInfo", error["operation"])

    def test_capture_is_unsupported_on_non_windows_without_backend_call(self) -> None:
        backend = FakeBackend()
        with self.assertRaises(WorkerMemoryUnsupportedError):
            capture_system_physical_memory(
                _platform="darwin", _backend=backend
            )


if __name__ == "__main__":
    unittest.main()
