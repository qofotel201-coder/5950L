from __future__ import annotations

from contextlib import redirect_stderr
import io
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cfdpipe.coarse_projection_worker import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    _HeavyDependencies,
    execute_projection_worker,
)


GIB = 1024**3
MIB = 1024**2


def _memory_report(kind: str) -> dict:
    return {"schema": f"test.{kind}", "status": "PASS", "metrics": {}}


class FakeMemoryApi:
    def __init__(self, events: list[str], *, fail_limit: bool = False) -> None:
        self.events = events
        self.fail_limit = fail_limit

    def capture_system_physical_memory(self) -> dict:
        self.events.append("system")
        return _memory_report("system")

    def capture_current_process_memory(self) -> dict:
        self.events.append("process")
        return _memory_report("process")

    def install_current_process_memory_limit(self, limit_bytes: int) -> dict:
        self.events.append(f"limit:{limit_bytes}")
        if self.fail_limit:
            error = RuntimeError("raw job assignment failure")
            error.evidence = {  # type: ignore[attr-defined]
                "status": "FAIL",
                "error": {"message": "raw job assignment failure"},
            }
            raise error
        return {
            "schema": "cfdpipe.worker_memory_limit.v1",
            "status": "PASS",
            "requested_process_memory_limit_bytes": limit_bytes,
            "job_object": {
                "process_memory_limit_bytes": limit_bytes,
                "process_assigned": True,
                "handle_retained_for_process_lifetime": True,
            },
        }


class FakeStrategy:
    events: list[str] = []

    def __init__(self, markers, topology, **kwargs) -> None:
        self.events.append("strategy")
        self.markers = markers
        self.topology = topology
        self.kwargs = kwargs


def _projection_manifest(*, status: str = "PASS") -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "status": status,
        "projection_only": True,
        "calibration_PASS_authorized": False,
        "production": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "started_at_utc": "2026-08-02T00:00:00Z",
        "ended_at_utc": "2026-08-02T00:00:01Z",
        "elapsed_seconds": 1.0,
        "gmsh_session": {
            "initialize_called": True,
            "finalize_called": True,
            "cleanup_errors": [],
        },
        "projection": {
            "projection_gate": {
                "projected_3d_elements": 299_000,
                "strictly_below_cap": status == "PASS",
            }
        },
        "error": None
        if status == "PASS"
        else {
            "type": "ProjectionGateError",
            "message": "projection count failed",
            "traceback": "full runner traceback",
        },
    }


class ProjectionWorkerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "config").mkdir()
        self.output_root = self.root / "runs" / "mesh" / "coarse"
        self.output_root.mkdir(parents=True)
        self.config = self.root / "config" / "coarse_mesh.toml"
        self.config.write_text(
            """
schema = "cfdpipe.coarse_mesh.v1"
status = "CONFIGURED"

[calibration]
characteristic_lengths_m = [0.30, 0.25]

[resource]
projection_worker_memory_limit_bytes = 1073741824
physical_memory_required = true
pagefile_cannot_rescue_physical_memory_failure = true
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (self.root / "config" / "markers.toml").write_text(
            "name = \"markers\"\n", encoding="utf-8"
        )
        (self.root / "config" / "topology_smoke.toml").write_text(
            "name = \"topology\"\n", encoding="utf-8"
        )
        FakeStrategy.events = []
        self.config_sha256 = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.contract_sha256 = "c" * 64

    def _heavy(self, events: list[str], *, status: str = "PASS"):
        def normalize(document, *, repository_root):
            events.append("normalize")
            self.assertEqual(self.root, repository_root)
            return {
                "normalized_config_sha256": self.contract_sha256,
                "resource": {
                    "projection_worker_memory_limit_bytes": 1 * GIB,
                },
                "calibration_characteristic_lengths_m": [0.30, 0.25],
                "provenance": {
                    "markers_sha256": "a" * 64,
                    "topology_smoke_sha256": "b" * 64,
                },
            }

        def runner(**kwargs):
            events.append("runner")
            self.assertAlmostEqual(0.30, kwargs["characteristic_length_m"])
            self.assertIsInstance(kwargs["strategy"], FakeStrategy)
            return _projection_manifest(status=status)

        def loader():
            events.append("heavy_import")
            FakeStrategy.events = events
            return _HeavyDependencies(normalize, runner, FakeStrategy)

        return loader

    def _run(
        self,
        name: str,
        *,
        memory: FakeMemoryApi,
        loader,
        characteristic: float = 0.30,
    ):
        output = self.output_root / name
        with redirect_stderr(io.StringIO()):
            code, manifest = execute_projection_worker(
                config_path=self.config,
                config_sha256=self.config_sha256,
                coarse_contract_sha256=self.contract_sha256,
                characteristic_length_m=characteristic,
                output_directory=output,
                argv=["python", "-m", "cfdpipe.coarse_projection_worker"],
                repository_root=self.root,
                output_root=self.output_root,
                memory_api=memory,
                heavy_loader=loader,
            )
        return code, manifest, output


class CoarseProjectionWorkerTests(ProjectionWorkerFixture):
    def test_hard_limit_precedes_every_heavy_import_and_manifest_is_atomic_json(self) -> None:
        events: list[str] = []
        memory = FakeMemoryApi(events)

        code, manifest, output = self._run(
            "projection_001", memory=memory, loader=self._heavy(events)
        )

        self.assertEqual(0, code)
        self.assertEqual("PASS", manifest["status"])
        self.assertLess(events.index(f"limit:{GIB}"), events.index("heavy_import"))
        self.assertEqual(
            ["system", "process", f"limit:{GIB}", "system", "process"],
            events[:5],
        )
        self.assertEqual(
            ["heavy_import", "normalize", "strategy", "runner", "process", "system"],
            events[5:],
        )
        path = output / MANIFEST_NAME
        self.assertTrue(path.is_file())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("PASS", saved["status"])
        self.assertTrue(
            saved["worker"]["hard_limit_installed_before_heavy_import"]
        )
        self.assertTrue(saved["worker"]["heavy_dependencies_imported"])
        self.assertEqual(GIB, saved["worker_memory"]["hard_limit"][
            "requested_process_memory_limit_bytes"
        ])
        self.assertEqual([], list(output.glob(".*.tmp")))
        json.dumps(saved, allow_nan=False)

    def test_limit_failure_writes_full_error_and_never_loads_heavy_modules(self) -> None:
        events: list[str] = []
        memory = FakeMemoryApi(events, fail_limit=True)

        code, manifest, output = self._run(
            "projection_limit_fail",
            memory=memory,
            loader=lambda: self.fail("heavy loader must not run"),
        )

        self.assertEqual(1, code)
        self.assertEqual("FAIL", manifest["status"])
        self.assertEqual(["system", "process", f"limit:{GIB}"], events)
        saved = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn("raw job assignment failure", saved["worker_error"]["message"])
        self.assertIn("Traceback", saved["worker_error"]["traceback"])
        self.assertEqual("FAIL", saved["worker_error"]["evidence"]["status"])
        self.assertFalse(saved["worker"]["heavy_dependencies_imported"])

    def test_runner_fail_manifest_preserves_finalize_and_original_traceback(self) -> None:
        events: list[str] = []

        code, manifest, output = self._run(
            "projection_runner_fail",
            memory=FakeMemoryApi(events),
            loader=self._heavy(events, status="FAIL"),
        )

        self.assertEqual(1, code)
        self.assertEqual("FAIL", manifest["status"])
        saved = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertTrue(saved["gmsh_session"]["finalize_called"])
        self.assertEqual("full runner traceback", saved["error"]["traceback"])
        self.assertIsNone(saved["worker_error"])

    def test_runner_exception_is_recorded_but_worker_never_attempts_gmsh_cleanup(self) -> None:
        events: list[str] = []

        def normalize(document, *, repository_root):
            return {
                "normalized_config_sha256": self.contract_sha256,
                "resource": {"projection_worker_memory_limit_bytes": GIB},
                "calibration_characteristic_lengths_m": [0.30],
                "provenance": {
                    "markers_sha256": "a" * 64,
                    "topology_smoke_sha256": "b" * 64,
                },
            }

        def runner(**kwargs):
            raise RuntimeError("runner escaped unexpectedly")

        def loader():
            events.append("heavy_import")
            return _HeavyDependencies(normalize, runner, FakeStrategy)

        code, manifest, output = self._run(
            "projection_runner_exception",
            memory=FakeMemoryApi(events),
            loader=loader,
        )

        self.assertEqual(1, code)
        self.assertEqual("RuntimeError", manifest["worker_error"]["type"])
        self.assertIn("runner escaped unexpectedly", manifest["worker_error"]["traceback"])
        saved = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertNotIn("gmsh_finalize", events)
        self.assertTrue(saved["worker"]["projection_runner_called"])

    def test_unauthorized_characteristic_length_stops_before_memory_and_output(self) -> None:
        events: list[str] = []
        output = self.output_root / "projection_bad_size"

        code, manifest, returned_output = self._run(
            "projection_bad_size",
            memory=FakeMemoryApi(events),
            loader=lambda: self.fail("heavy loader must not run"),
            characteristic=0.20,
        )

        self.assertEqual(output, returned_output)
        self.assertEqual(1, code)
        self.assertEqual([], events)
        self.assertFalse(output.exists())
        self.assertIn("not authorized", manifest["worker_error"]["message"])

    def test_existing_output_is_never_overwritten(self) -> None:
        output = self.output_root / "existing"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        events: list[str] = []

        with redirect_stderr(io.StringIO()):
            code, manifest = execute_projection_worker(
                config_path=self.config,
                config_sha256=self.config_sha256,
                coarse_contract_sha256=self.contract_sha256,
                characteristic_length_m=0.30,
                output_directory=output,
                argv=["worker"],
                repository_root=self.root,
                output_root=self.output_root,
                memory_api=FakeMemoryApi(events),
                heavy_loader=lambda: self.fail("heavy loader must not run"),
            )

        self.assertEqual(1, code)
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
        self.assertEqual([], events)
        self.assertEqual("FAIL", manifest["status"])

    def test_normalized_limit_mismatch_fails_after_limit_but_before_runner(self) -> None:
        events: list[str] = []

        def normalize(document, *, repository_root):
            events.append("normalize")
            return {
                "normalized_config_sha256": self.contract_sha256,
                "resource": {"projection_worker_memory_limit_bytes": 2 * GIB},
                "calibration_characteristic_lengths_m": [0.30],
                "provenance": {
                    "markers_sha256": "a" * 64,
                    "topology_smoke_sha256": "b" * 64,
                },
            }

        def loader():
            events.append("heavy_import")
            return _HeavyDependencies(
                normalize,
                lambda **kwargs: self.fail("runner must not run"),
                FakeStrategy,
            )

        code, manifest, output = self._run(
            "projection_limit_mismatch",
            memory=FakeMemoryApi(events),
            loader=loader,
        )

        self.assertEqual(1, code)
        self.assertIn("changed", manifest["worker_error"]["message"])
        self.assertTrue((output / MANIFEST_NAME).is_file())
        self.assertLess(events.index(f"limit:{GIB}"), events.index("normalize"))


if __name__ == "__main__":
    unittest.main()
