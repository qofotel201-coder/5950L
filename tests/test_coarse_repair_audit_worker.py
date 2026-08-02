from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

from cfdpipe import coarse_repair_audit_worker as worker
from cfdpipe.coarse_direction_replay import (
    make_physical_schedule_homotopy_endpoint,
)
from cfdpipe.coarse_schedule_continuation import (
    DISCOVERY_SCHEMA as SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA,
)


class CoarseRepairAuditWorkerTests(unittest.TestCase):
    GIB = 1024**3

    class _Signals:
        SIGBREAK = 21

        def __init__(self):
            self.original_handler = object()
            self.current_handler = self.original_handler
            self.calls = []

        def getsignal(self, signum):
            self.calls.append(("getsignal", signum))
            return self.current_handler

        def signal(self, signum, handler):
            self.calls.append(("signal", signum, handler))
            previous = self.current_handler
            self.current_handler = handler
            return previous

    class _Memory:
        def __init__(self, events, *, fail_limit=False):
            self.events = events
            self.fail_limit = fail_limit

        def capture_system_physical_memory(self):
            self.events.append("system")
            return {"schema": "test.system", "status": "PASS", "metrics": {}}

        def capture_current_process_memory(self):
            self.events.append("process")
            return {"schema": "test.process", "status": "PASS", "metrics": {}}

        def install_current_process_memory_limit(self, limit_bytes):
            self.events.append(f"limit:{limit_bytes}")
            if self.fail_limit:
                raise RuntimeError("job failed")
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

    class _Strategy:
        events = []

        def __init__(self, markers, topology, **kwargs):
            del markers, topology, kwargs
            self.events.append("strategy")

    def _fixture(self, root: Path):
        (root / "config").mkdir()
        output_root = root / "runs" / "mesh" / "coarse"
        output_root.mkdir(parents=True)
        config = root / "config" / "coarse_mesh.toml"
        config.write_text(
            """
schema = "cfdpipe.coarse_mesh.v1"
status = "CONFIGURED"

[calibration]
characteristic_lengths_m = [0.40, 0.25]

[resource]
minimum_calibration_samples = 2
safety_factor = 1.30
os_physical_memory_reserve_bytes = 4294967296
minimum_disk_free_bytes = 21474836480
physical_memory_required = true
pagefile_cannot_rescue_physical_memory_failure = true
projection_worker_memory_limit_bytes = 2147483648
calibration_worker_memory_limit_bytes = 3221225472
worker_monitor_margin_bytes = 536870912
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (root / "config" / "markers.toml").write_text(
            'name = "markers"\n', encoding="utf-8"
        )
        (root / "config" / "topology_smoke.toml").write_text(
            'name = "topology"\n', encoding="utf-8"
        )
        resource = tomllib.loads(config.read_text("utf-8"))["resource"]
        schedule_dir = output_root / "schedule"
        schedule_dir.mkdir()
        schedule = schedule_dir / "boundary_layer_local_schedule.json"
        schedule.write_text('{"status":"PASS"}\n', encoding="utf-8")
        schedule_hash = hashlib.sha256(schedule.read_bytes()).hexdigest()
        projection_dir = output_root / "projection"
        projection_dir.mkdir()
        projection = projection_dir / "projection_manifest.json"
        projection.write_text('{"status":"PASS"}\n', encoding="utf-8")
        self.config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        self.contract_hash = "d" * 64
        self.projection = projection
        self.projection_hash = hashlib.sha256(projection.read_bytes()).hexdigest()
        return config, output_root, resource, schedule, schedule_hash

    def _projection_loader(self, path, digest, **kwargs):
        self.assertEqual(self.projection, Path(path))
        self.assertEqual(self.projection_hash, digest)
        self.assertEqual(0.4, kwargs["expected_characteristic_length_m"])
        return {
            "schema": "cfdpipe.coarse_mesh_projection_evidence.v1",
            "status": "PASS",
            "path": str(self.projection.resolve()),
            "sha256": self.projection_hash,
            "size_bytes": self.projection.stat().st_size,
            "config_sha256": self.config_hash,
            "coarse_contract_sha256": self.contract_hash,
            "characteristic_length_m": 0.4,
            "projected_3d_elements": 294059,
            "evidence_sha256": "f" * 64,
        }

    def _lineage(self):
        return {
            "config_sha256": self.config_hash,
            "coarse_contract_sha256": self.contract_hash,
            "projection_manifest_path": self.projection,
            "projection_manifest_sha256": self.projection_hash,
            "projection_evidence_loader": self._projection_loader,
        }

    def _heavy(
        self,
        events,
        resource,
        *,
        result_status="PASS",
        replay_approval=None,
        replay_homotopy_endpoint=None,
        replay_continuation_endpoint=None,
        raise_audit=False,
        audit_hook=None,
    ):
        def normalize(document, *, repository_root):
            del document, repository_root
            events.append("normalize")
            return {
                "normalized_config_sha256": self.contract_hash,
                "resource": copy.deepcopy(resource),
                "calibration_characteristic_lengths_m": [0.4, 0.25],
                "provenance": {
                    "markers_sha256": "a" * 64,
                    "topology_smoke_sha256": "b" * 64,
                },
                "quality": {
                    "minimum_prism_scaled_jacobian": 0.01,
                    "minimum_core_tetra_gamma": 0.001,
                },
                "boundary_layer_design": {
                    "first_layer_height_m": 1.0e-6,
                    "layer_count": 60,
                },
                "wall_surface_fingerprints": [
                    f"{index:064x}" for index in range(48)
                ],
            }

        def bind(*, coarse_contract, plan_path, plan_sha256):
            del coarse_contract
            events.append("bind")
            source = Path(plan_path).resolve(strict=True)
            return {
                "schema": "cfdpipe.boundary_layer_local_schedule_binding.v1",
                "status": "PASS",
                "source_plan_path": str(source),
                "source_plan_sha256": plan_sha256,
                "source_plan_size_bytes": source.stat().st_size,
                "coarse_contract_sha256": self.contract_hash,
                "surface_count": 48,
                "layer_count": 60,
                "binding_sha256": "e" * 64,
            }

        def audit_runner(**kwargs):
            events.append("audit")
            if audit_hook is not None:
                audit_hook()
            if raise_audit:
                raise RuntimeError("physical audit infrastructure failed")
            supplied_approval = kwargs.get("direction_replay_approval")
            if supplied_approval is not None:
                self.assertEqual(replay_approval, supplied_approval)
                self.assertEqual(
                    supplied_approval["approval_sha256"],
                    kwargs["run_evidence"][
                        "direction_replay_approval_sha256"
                    ],
                )
                complete = result_status == "PASS"
                homotopy_requested = (
                    kwargs.get("schedule_feasibility_endpoint")
                    == worker.PHYSICAL_HOMOTOPY_REQUEST
                )
                continuation_requested = (
                    kwargs.get("schedule_feasibility_endpoint")
                    == worker.SCHEDULE_CONTINUATION_REQUEST
                )
                combined_requested = (
                    homotopy_requested or continuation_requested
                )
                homotopy_endpoint = (
                    replay_homotopy_endpoint if combined_requested else None
                )
                if combined_requested:
                    self.assertEqual(
                        homotopy_endpoint["endpoint_sha256"],
                        kwargs["run_evidence"][
                            "physical_schedule_homotopy_endpoint_sha256"
                        ],
                    )
                return {
                    "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
                    "status": result_status,
                    "error": None,
                    "audit_purpose": (
                        worker.SCHEDULE_CONTINUATION_PURPOSE
                        if continuation_requested
                        else (
                            worker.PHYSICAL_HOMOTOPY_PURPOSE
                            if homotopy_requested
                            else "physical_local_schedule_fixed_direction_replay"
                        )
                    ),
                    "schedule_feasibility_endpoint_requested": (
                        kwargs.get("schedule_feasibility_endpoint")
                    ),
                    "direction_replay_approval": copy.deepcopy(
                        supplied_approval
                    ),
                    "physical_schedule_homotopy_endpoint": copy.deepcopy(
                        homotopy_endpoint
                    ),
                    "schedule_direction_continuation_endpoint": copy.deepcopy(
                        replay_continuation_endpoint
                        if continuation_requested
                        else None
                    ),
                    "audit_only": True,
                    "calibration_PASS_authorized": False,
                    "production_mesh_eligible": False,
                    "mesh_written": False,
                    "su2_called": False,
                    "paraview_called": False,
                    "external_commands": [],
                    "repair_discovery": {
                        "schema": (
                            "cfdpipe.coarse_physical_schedule_fixed_direction_"
                            "homotopy_discovery.v1"
                            if homotopy_requested
                            else (
                                SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA
                                if continuation_requested
                                else "cfdpipe.coarse_physical_schedule_fixed_"
                                "direction_discovery.v1"
                            )
                        ),
                        "status": result_status,
                        "profile_complete": complete,
                    },
                }
            if "schedule_feasibility_endpoint" in kwargs:
                events.append(
                    "endpoint:" + kwargs["schedule_feasibility_endpoint"]
                )
                self.assertEqual(
                    kwargs["schedule_feasibility_endpoint"],
                    kwargs["run_evidence"]["schedule_feasibility_endpoint"],
                )
            self.assertEqual("PASS", kwargs["local_schedule_binding"]["status"])
            complete = result_status == "PASS"
            requested_endpoint = kwargs.get("schedule_feasibility_endpoint")
            direction_family = requested_endpoint in {
                "minimum-growth-owner-free-component-direction-audit",
                "minimum-growth-owner-free-component-pattern-audit",
            }
            discovery_schema = (
                "cfdpipe.coarse_component_direction_discovery.v1"
                if direction_family
                else "cfdpipe.coarse_repair_discovery.v1"
            )
            audit_purpose = (
                "repair_profile_discovery"
                if requested_endpoint is None
                else (
                    "minimum_growth_plus_existing_direction_repair"
                    if requested_endpoint
                    == "minimum-growth-existing-direction-repair"
                    else (
                        "minimum_growth_owner_free_component_direction_audit"
                        if requested_endpoint
                        == "minimum-growth-owner-free-component-direction-audit"
                        else "minimum_growth_owner_free_component_pattern_audit"
                    )
                )
            )
            return {
                "schema": "cfdpipe.coarse_repair_audit_manifest.v1",
                "status": result_status,
                "audit_purpose": audit_purpose,
                "schedule_feasibility_endpoint_requested": requested_endpoint,
                "audit_only": True,
                "calibration_PASS_authorized": False,
                "production_mesh_eligible": False,
                "mesh_written": False,
                "su2_called": False,
                "paraview_called": False,
                "external_commands": [],
                "repair_discovery": {
                    "schema": discovery_schema,
                    "status": result_status,
                    "profile_complete": complete,
                },
            }

        def loader():
            events.append("heavy_import")
            self._Strategy.events = events
            return worker._HeavyDependencies(
                normalize, bind, self._Strategy, audit_runner
            )

        return loader

    def _execute(self, root, output, events, resource, schedule, schedule_hash, **kw):
        replay_approval = kw.pop("replay_approval", None)
        replay_homotopy_endpoint = kw.pop(
            "replay_homotopy_endpoint", None
        )
        replay_continuation_endpoint = kw.pop(
            "replay_continuation_endpoint", None
        )
        return worker.execute_repair_audit_worker(
            config_path=root / "config" / "coarse_mesh.toml",
            **self._lineage(),
            characteristic_length_m=kw.pop("characteristic", 0.4),
            local_schedule_path=schedule,
            local_schedule_sha256=schedule_hash,
            output_directory=output,
            argv=["python", "-m", "cfdpipe.coarse_repair_audit_worker"],
            repository_root=root,
            output_root=root / "runs" / "mesh" / "coarse",
            memory_api=self._Memory(events, fail_limit=kw.pop("fail_limit", False)),
            heavy_loader=self._heavy(
                events,
                resource,
                result_status=kw.pop("result_status", "PASS"),
                replay_approval=replay_approval,
                replay_homotopy_endpoint=replay_homotopy_endpoint,
                replay_continuation_endpoint=replay_continuation_endpoint,
                raise_audit=kw.pop("raise_audit", False),
                audit_hook=kw.pop("audit_hook", None),
            ),
            **kw,
        )

    def _replay_fixture(self, output_root: Path):
        sources = []
        for ordinal in (1, 2):
            path = (
                output_root
                / f"direction-source-{ordinal}"
                / worker.MANIFEST_NAME
            )
            path.parent.mkdir()
            path.write_text(
                json.dumps({"source": ordinal}, allow_nan=False),
                encoding="utf-8",
            )
            sources.append(
                (path, hashlib.sha256(path.read_bytes()).hexdigest())
            )
        approval = {
            "approval_sha256": "9" * 64,
            "source_minimum_growth_final_quality": {
                "prism_element_count": 280320,
                "core_element_count": 13739,
            },
            "source_audit_manifests": [
                {
                    "path": str(path.resolve()),
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                }
                for path, digest in sources
            ],
        }
        return sources, approval

    def _replay_imports(
        self,
        approval,
        homotopy_endpoint=None,
        *,
        homotopy_evidence=None,
        continuation_endpoint=None,
    ):
        actual_import = worker.importlib.import_module
        audit_module = SimpleNamespace(
            make_coarse_repair_audit_strategy_config=(
                lambda *args, **kwargs: {
                    "strategy": "minimum-growth-pattern",
                    "arguments": len(args),
                    "endpoint": kwargs.get("schedule_feasibility_endpoint"),
                }
            ),
            _physical_surface_schedule_values=(
                lambda *args, **kwargs: {
                    f"{index:064x}": [1.0e-6] * 60
                    for index in range(48)
                }
            ),
            load_and_validate_physical_homotopy_audit_manifest=(
                lambda *args, **kwargs: copy.deepcopy(homotopy_evidence)
            ),
        )
        replay_module = SimpleNamespace(
            load_direction_replay_consensus_approval=(
                lambda *args, **kwargs: copy.deepcopy(approval)
            ),
            make_physical_schedule_homotopy_endpoint=(
                lambda **kwargs: copy.deepcopy(homotopy_endpoint)
            ),
            validate_physical_schedule_homotopy_endpoint=(
                lambda value, **kwargs: copy.deepcopy(dict(value))
            ),
        )
        continuation_module = SimpleNamespace(
            DISCOVERY_SCHEMA=SCHEDULE_CONTINUATION_DISCOVERY_SCHEMA,
            make_schedule_frontier_direction_endpoint=(
                lambda **kwargs: copy.deepcopy(continuation_endpoint)
            ),
            validate_schedule_frontier_direction_endpoint=(
                lambda value, **kwargs: copy.deepcopy(dict(value))
            ),
        )

        def import_module(name):
            if name == "cfdpipe.coarse_repair_audit":
                return audit_module
            if name == "cfdpipe.coarse_direction_replay":
                return replay_module
            if name == "cfdpipe.coarse_schedule_continuation":
                return continuation_module
            return actual_import(name)

        return mock.patch.object(
            worker.importlib, "import_module", side_effect=import_module
        )

    @staticmethod
    def _homotopy_endpoint():
        return make_physical_schedule_homotopy_endpoint(
            baseline_binding_sha256="e" * 64,
            first_layer_height_m=1.0e-6,
            layer_count=60,
        )

    def test_job_is_installed_before_heavy_import_and_pass_is_persisted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            config, output_root, resource, schedule, digest = self._fixture(root)
            del config
            output = output_root / "audit-pass"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root, output, events, resource, schedule, digest
                )
            self.assertEqual(0, code)
            self.assertLess(
                events.index(f"limit:{3 * self.GIB}"), events.index("heavy_import")
            )
            self.assertEqual("PASS", manifest["worker_isolation"]["status"])
            self.assertTrue((output / worker.MANIFEST_NAME).is_file())
            evidence = json.loads(
                (
                    output_root
                    / "_worker_evidence"
                    / output.name
                    / worker.WORKER_EVIDENCE_NAME
                ).read_text("utf-8")
            )
            self.assertEqual("PASS", evidence["status"])
            self.assertTrue(
                evidence["worker"]["hard_limit_installed_before_heavy_import"]
            )
            self.assertTrue(evidence["worker"]["audit_runner_called"])

    def test_sigbreak_handler_installs_once_and_restores_original_handler(self):
        signals = self._Signals()
        restore = worker._install_controlled_sigbreak_handler(
            platform="win32", signal_module=signals
        )
        self.assertIsNotNone(restore)
        self.assertIsNot(signals.original_handler, signals.current_handler)
        with self.assertRaises(worker.CoarseRepairAuditWorkerCancelled):
            signals.current_handler(signals.SIGBREAK, None)
        restore()
        restore()
        self.assertIs(signals.original_handler, signals.current_handler)
        self.assertEqual(
            2,
            sum(call[0] == "signal" for call in signals.calls),
        )

    def test_sigbreak_handler_is_unavailable_without_sigbreak(self):
        signal_module = SimpleNamespace(
            getsignal=mock.Mock(),
            signal=mock.Mock(),
        )
        restore = worker._install_controlled_sigbreak_handler(
            platform="win32", signal_module=signal_module
        )
        self.assertIsNone(restore)
        signal_module.getsignal.assert_not_called()
        signal_module.signal.assert_not_called()

    def test_sigbreak_cancellation_runs_audit_finally_and_persists_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            output = output_root / "sigbreak-cancel"
            events = []
            signals = self._Signals()

            def cancel_from_audit():
                try:
                    signals.current_handler(signals.SIGBREAK, None)
                finally:
                    events.append("audit-finally")

            with contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    audit_hook=cancel_from_audit,
                    sigbreak_installer=lambda: (
                        worker._install_controlled_sigbreak_handler(
                            platform="win32", signal_module=signals
                        )
                    ),
                )
            self.assertEqual(1, code)
            self.assertIn("audit-finally", events)
            self.assertIs(signals.original_handler, signals.current_handler)
            self.assertEqual("FAIL", failure["status"])
            self.assertEqual(
                "CoarseRepairAuditWorkerCancelled",
                failure["worker_error"]["type"],
            )
            evidence = json.loads(
                (
                    output_root
                    / "_worker_evidence"
                    / output.name
                    / worker.WORKER_EVIDENCE_NAME
                ).read_text("utf-8")
            )
            self.assertEqual("FAIL", evidence["status"])
            self.assertEqual(
                "CoarseRepairAuditWorkerCancelled",
                evidence["worker_error"]["type"],
            )

    def test_incomplete_profile_returns_nonzero_and_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            output = output_root / "audit-incomplete"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    result_status="INCOMPLETE",
                )
            self.assertEqual(1, code)
            self.assertEqual("INCOMPLETE", manifest["status"])
            self.assertTrue((output / worker.MANIFEST_NAME).is_file())
            self.assertEqual("FAIL", manifest["worker_isolation"]["status"])

    def test_job_failure_stops_before_heavy_import(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            output = output_root / "job-fail"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, result = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    fail_limit=True,
                )
            self.assertEqual(1, code)
            self.assertNotIn("heavy_import", events)
            self.assertFalse(output.exists())
            self.assertIn("job failed", result["worker_error"]["message"])

    def test_second_point_is_rejected_before_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, result = self._execute(
                    root,
                    output_root / "second",
                    events,
                    resource,
                    schedule,
                    digest,
                    characteristic=0.25,
                )
            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertIn("first configured point", result["worker_error"]["message"])

    def test_parser_requires_projection_and_local_schedule_lineage(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            worker._parser().parse_args(
                [
                    "--config",
                    "config/coarse_mesh.toml",
                    "--config-sha256",
                    "a" * 64,
                    "--coarse-contract-sha256",
                    "b" * 64,
                    "--characteristic-length",
                    "0.4",
                    "--output",
                    "runs/mesh/coarse/audit",
                ]
            )

    def test_minimum_growth_endpoint_is_forwarded_with_bound_run_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            output = output_root / "endpoint-pass"
            events = []
            endpoint = "minimum-growth-existing-direction-repair"
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    schedule_feasibility_endpoint=endpoint,
                )
            self.assertEqual(0, code)
            self.assertIn(f"endpoint:{endpoint}", events)
            self.assertEqual("PASS", manifest["worker_isolation"]["status"])

    def test_pattern_endpoint_is_parsed_forwarded_and_uses_component_schema(self):
        endpoint = "minimum-growth-owner-free-component-pattern-audit"
        parsed = worker._parser().parse_args(
            [
                "--config",
                "config/coarse_mesh.toml",
                "--config-sha256",
                "a" * 64,
                "--coarse-contract-sha256",
                "b" * 64,
                "--characteristic-length",
                "0.4",
                "--projection-manifest",
                "runs/mesh/coarse/projection/projection_manifest.json",
                "--projection-sha256",
                "c" * 64,
                "--local-schedule",
                "runs/mesh/coarse/schedule/boundary_layer_local_schedule.json",
                "--local-schedule-sha256",
                "d" * 64,
                "--schedule-feasibility-endpoint",
                endpoint,
                "--output",
                "runs/mesh/coarse/pattern",
            ]
        )
        self.assertEqual(endpoint, parsed.schedule_feasibility_endpoint)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            output = output_root / "pattern-pass"
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    schedule_feasibility_endpoint=endpoint,
                )
            self.assertEqual(0, code)
            self.assertIn(f"endpoint:{endpoint}", events)
            self.assertEqual(
                "minimum_growth_owner_free_component_pattern_audit",
                manifest["audit_purpose"],
            )
            self.assertEqual(
                "cfdpipe.coarse_component_direction_discovery.v1",
                manifest["repair_discovery"]["schema"],
            )
            self.assertEqual("PASS", manifest["worker_isolation"]["status"])

    def test_physical_replay_incomplete_returns_two_and_preserves_attested_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            output = output_root / "physical-incomplete"
            events = []
            with self._replay_imports(approval), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    result_status="INCOMPLETE",
                    replay_approval=approval,
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                )
            self.assertEqual(2, code)
            self.assertEqual("INCOMPLETE", manifest["status"])
            self.assertEqual(
                "INCOMPLETE", manifest["repair_discovery"]["status"]
            )
            isolation = manifest["worker_isolation"]
            self.assertEqual("PASS", isolation["status"])
            self.assertEqual("INCOMPLETE", isolation["audit_result_status"])
            self.assertIsNone(isolation["worker_error"])
            self.assertEqual(
                approval, isolation["direction_replay"]["approval"]
            )
            self.assertEqual(
                [str(item[0].resolve()) for item in sources],
                [
                    item["path"]
                    for item in isolation["direction_replay"][
                        "pre_job_sources"
                    ]
                ],
            )
            self.assertTrue((output / worker.MANIFEST_NAME).is_file())

    def test_physical_homotopy_is_parsed_and_legal_incomplete_returns_two(self):
        endpoint = self._homotopy_endpoint()
        parsed = worker._parser().parse_args(
            [
                "--config",
                "config/coarse_mesh.toml",
                "--config-sha256",
                "a" * 64,
                "--coarse-contract-sha256",
                "b" * 64,
                "--characteristic-length",
                "0.4",
                "--projection-manifest",
                "runs/mesh/coarse/projection/projection_manifest.json",
                "--projection-sha256",
                "c" * 64,
                "--local-schedule",
                "runs/mesh/coarse/schedule/boundary_layer_local_schedule.json",
                "--local-schedule-sha256",
                "d" * 64,
                "--schedule-feasibility-endpoint",
                worker.PHYSICAL_HOMOTOPY_REQUEST,
                "--physical-schedule-homotopy-endpoint-sha256",
                endpoint["endpoint_sha256"],
                "--output",
                "runs/mesh/coarse/homotopy",
            ]
        )
        self.assertEqual(
            worker.PHYSICAL_HOMOTOPY_REQUEST,
            parsed.schedule_feasibility_endpoint,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            output = output_root / "physical-homotopy-incomplete"
            events = []
            with self._replay_imports(
                approval, endpoint
            ), contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    result_status="INCOMPLETE",
                    replay_approval=approval,
                    replay_homotopy_endpoint=endpoint,
                    schedule_feasibility_endpoint=(
                        worker.PHYSICAL_HOMOTOPY_REQUEST
                    ),
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                    physical_schedule_homotopy_endpoint_sha256=endpoint[
                        "endpoint_sha256"
                    ],
                )
            self.assertEqual(2, code)
            self.assertEqual("INCOMPLETE", manifest["status"])
            self.assertEqual(
                worker.PHYSICAL_HOMOTOPY_PURPOSE,
                manifest["audit_purpose"],
            )
            self.assertEqual(
                endpoint, manifest["physical_schedule_homotopy_endpoint"]
            )
            self.assertEqual(
                endpoint,
                manifest["worker_isolation"]["direction_replay"][
                    "homotopy_endpoint"
                ],
            )
            self.assertEqual(
                "PASS", manifest["worker_isolation"]["status"]
            )
            self.assertEqual(
                "INCOMPLETE",
                manifest["worker_isolation"]["audit_result_status"],
            )

    def test_physical_homotopy_endpoint_mismatch_fails_before_audit(self):
        endpoint = self._homotopy_endpoint()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            events = []
            with self._replay_imports(
                approval, endpoint
            ), contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output_root / "physical-homotopy-mismatch",
                    events,
                    resource,
                    schedule,
                    digest,
                    replay_approval=approval,
                    replay_homotopy_endpoint=endpoint,
                    schedule_feasibility_endpoint=(
                        worker.PHYSICAL_HOMOTOPY_REQUEST
                    ),
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                    physical_schedule_homotopy_endpoint_sha256="8" * 64,
                )
            self.assertEqual(1, code)
            self.assertIn(f"limit:{3 * self.GIB}", events)
            self.assertIn("heavy_import", events)
            self.assertNotIn("audit", events)
            self.assertIn(
                "homotopy endpoint differs",
                failure["worker_error"]["message"],
            )

    def test_physical_homotopy_without_replay_sources_fails_before_job(self):
        endpoint = self._homotopy_endpoint()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output_root / "physical-homotopy-no-replay",
                    events,
                    resource,
                    schedule,
                    digest,
                    schedule_feasibility_endpoint=(
                        worker.PHYSICAL_HOMOTOPY_REQUEST
                    ),
                    physical_schedule_homotopy_endpoint_sha256=endpoint[
                        "endpoint_sha256"
                    ],
                )
            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertIn(
                "homotopy endpoint inputs are incomplete",
                failure["worker_error"]["message"],
            )

    def test_first_frontier_direction_inputs_are_parsed_explicitly(self):
        parsed = worker._parser().parse_args(
            [
                "--config",
                "config/coarse_mesh.toml",
                "--config-sha256",
                "a" * 64,
                "--coarse-contract-sha256",
                "b" * 64,
                "--characteristic-length",
                "0.4",
                "--projection-manifest",
                "runs/mesh/coarse/projection/projection_manifest.json",
                "--projection-sha256",
                "c" * 64,
                "--local-schedule",
                "runs/mesh/coarse/schedule/boundary_layer_local_schedule.json",
                "--local-schedule-sha256",
                "d" * 64,
                "--schedule-feasibility-endpoint",
                worker.SCHEDULE_CONTINUATION_REQUEST,
                "--physical-schedule-homotopy-endpoint-sha256",
                "e" * 64,
                "--homotopy-audit-manifest",
                "runs/mesh/coarse/homotopy/coarse_repair_audit_manifest.json",
                "--homotopy-audit-manifest-sha256",
                "f" * 64,
                "--schedule-direction-continuation-endpoint-sha256",
                "1" * 64,
                "--output",
                "runs/mesh/coarse/frontier-direction",
            ]
        )
        self.assertEqual(
            worker.SCHEDULE_CONTINUATION_REQUEST,
            parsed.schedule_feasibility_endpoint,
        )
        self.assertEqual("f" * 64, parsed.homotopy_audit_manifest_sha256)
        self.assertEqual(
            "1" * 64,
            parsed.schedule_direction_continuation_endpoint_sha256,
        )

    def test_first_frontier_direction_incomplete_inputs_fail_before_job(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output_root / "frontier-direction-incomplete",
                    events,
                    resource,
                    schedule,
                    digest,
                    schedule_feasibility_endpoint=(
                        worker.SCHEDULE_CONTINUATION_REQUEST
                    ),
                    physical_schedule_homotopy_endpoint_sha256="e" * 64,
                )
            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertIn(
                "homotopy endpoint inputs are incomplete",
                failure["worker_error"]["message"],
            )

    def test_first_frontier_direction_is_independently_rebuilt_and_returns_two(self):
        homotopy_endpoint = self._homotopy_endpoint()
        continuation_endpoint = {
            "schema": "test.frontier-direction-endpoint.v1",
            "endpoint_sha256": "6" * 64,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            homotopy_source = (
                output_root
                / "physical-homotopy-source"
                / worker.MANIFEST_NAME
            )
            homotopy_source.parent.mkdir()
            homotopy_source.write_text(
                '{"status":"INCOMPLETE"}\n', encoding="utf-8"
            )
            homotopy_sha256 = hashlib.sha256(
                homotopy_source.read_bytes()
            ).hexdigest()
            homotopy_evidence = {
                "path": str(homotopy_source.resolve()),
                "sha256": homotopy_sha256,
                "size_bytes": homotopy_source.stat().st_size,
                "manifest_status": "INCOMPLETE",
                "discovery": {"validated": True},
            }
            output = output_root / "frontier-direction-incomplete"
            events = []
            with self._replay_imports(
                approval,
                homotopy_endpoint,
                homotopy_evidence=homotopy_evidence,
                continuation_endpoint=continuation_endpoint,
            ), contextlib.redirect_stderr(io.StringIO()):
                code, manifest = self._execute(
                    root,
                    output,
                    events,
                    resource,
                    schedule,
                    digest,
                    result_status="INCOMPLETE",
                    replay_approval=approval,
                    replay_homotopy_endpoint=homotopy_endpoint,
                    replay_continuation_endpoint=continuation_endpoint,
                    schedule_feasibility_endpoint=(
                        worker.SCHEDULE_CONTINUATION_REQUEST
                    ),
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                    physical_schedule_homotopy_endpoint_sha256=(
                        homotopy_endpoint["endpoint_sha256"]
                    ),
                    homotopy_audit_manifest_path=homotopy_source,
                    homotopy_audit_manifest_sha256=homotopy_sha256,
                    schedule_direction_continuation_endpoint_sha256=(
                        continuation_endpoint["endpoint_sha256"]
                    ),
                )
            self.assertEqual(2, code)
            self.assertIn("audit", events)
            self.assertEqual(
                worker.SCHEDULE_CONTINUATION_PURPOSE,
                manifest["audit_purpose"],
            )
            replay = manifest["worker_isolation"]["direction_replay"]
            self.assertEqual(
                continuation_endpoint,
                replay["schedule_direction_continuation_endpoint"],
            )
            self.assertEqual(
                {
                    key: homotopy_evidence[key]
                    for key in (
                        "path",
                        "sha256",
                        "size_bytes",
                        "manifest_status",
                    )
                },
                replay["homotopy_audit_source"],
            )

    def test_first_frontier_direction_endpoint_mismatch_stops_before_audit(self):
        homotopy_endpoint = self._homotopy_endpoint()
        continuation_endpoint = {
            "schema": "test.frontier-direction-endpoint.v1",
            "endpoint_sha256": "6" * 64,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            homotopy_source = output_root / "homotopy" / worker.MANIFEST_NAME
            homotopy_source.parent.mkdir()
            homotopy_source.write_text("{}\n", encoding="utf-8")
            homotopy_sha256 = hashlib.sha256(
                homotopy_source.read_bytes()
            ).hexdigest()
            homotopy_evidence = {
                "path": str(homotopy_source.resolve()),
                "sha256": homotopy_sha256,
                "size_bytes": homotopy_source.stat().st_size,
                "manifest_status": "INCOMPLETE",
                "discovery": {"validated": True},
            }
            events = []
            with self._replay_imports(
                approval,
                homotopy_endpoint,
                homotopy_evidence=homotopy_evidence,
                continuation_endpoint=continuation_endpoint,
            ), contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output_root / "frontier-direction-mismatch",
                    events,
                    resource,
                    schedule,
                    digest,
                    replay_approval=approval,
                    replay_homotopy_endpoint=homotopy_endpoint,
                    schedule_feasibility_endpoint=(
                        worker.SCHEDULE_CONTINUATION_REQUEST
                    ),
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                    physical_schedule_homotopy_endpoint_sha256=(
                        homotopy_endpoint["endpoint_sha256"]
                    ),
                    homotopy_audit_manifest_path=homotopy_source,
                    homotopy_audit_manifest_sha256=homotopy_sha256,
                    schedule_direction_continuation_endpoint_sha256=(
                        "7" * 64
                    ),
                )
            self.assertEqual(1, code)
            self.assertNotIn("audit", events)
            self.assertIn(
                "continuation endpoint differs",
                failure["worker_error"]["message"],
            )

    def test_physical_replay_source_tamper_fails_before_job_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            events = []
            with contextlib.redirect_stderr(io.StringIO()):
                code, failure = self._execute(
                    root,
                    output_root / "physical-source-tamper",
                    events,
                    resource,
                    schedule,
                    digest,
                    replay_approval=approval,
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256="8" * 64,
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                )
            self.assertEqual(1, code)
            self.assertEqual([], events)
            self.assertEqual("FAIL", failure["status"])
            self.assertIn(
                "direction audit file identity",
                failure["worker_error"]["message"],
            )

    def test_physical_replay_approval_mismatch_fails_after_job_before_audit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            controller_approval_sha = "7" * 64
            events = []
            with self._replay_imports(approval), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code, failure = self._execute(
                    root,
                    output_root / "physical-approval-mismatch",
                    events,
                    resource,
                    schedule,
                    digest,
                    replay_approval=approval,
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=controller_approval_sha,
                )
            self.assertEqual(1, code)
            self.assertIn(f"limit:{3 * self.GIB}", events)
            self.assertIn("heavy_import", events)
            self.assertNotIn("audit", events)
            self.assertEqual("FAIL", failure["status"])
            self.assertIn(
                "approval differs",
                failure["worker_error"]["message"],
            )

    def test_physical_replay_infrastructure_error_remains_fail(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, output_root, resource, schedule, digest = self._fixture(root)
            sources, approval = self._replay_fixture(output_root)
            events = []
            with self._replay_imports(approval), contextlib.redirect_stderr(
                io.StringIO()
            ):
                code, failure = self._execute(
                    root,
                    output_root / "physical-infrastructure-fail",
                    events,
                    resource,
                    schedule,
                    digest,
                    replay_approval=approval,
                    raise_audit=True,
                    direction_audit_primary_path=sources[0][0],
                    direction_audit_primary_sha256=sources[0][1],
                    direction_audit_confirmation_path=sources[1][0],
                    direction_audit_confirmation_sha256=sources[1][1],
                    direction_replay_approval_sha256=approval[
                        "approval_sha256"
                    ],
                )
            self.assertEqual(1, code)
            self.assertIn("audit", events)
            self.assertEqual("FAIL", failure["status"])
            self.assertIn(
                "physical audit infrastructure failed",
                failure["worker_error"]["message"],
            )


if __name__ == "__main__":
    unittest.main()
